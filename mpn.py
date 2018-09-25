from copy import deepcopy
from typing import List, Tuple

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
import torch
import torch.nn.functional as F
import torch.nn as nn

from nn_utils import index_select_ND

# Atom feature sizes
ATOM_FEATURES = {
    'atomic_num': list(range(100)),
    'degree': [0, 1, 2, 3, 4, 5],
    'formal_charge': [-1, -2, 1, 2, 0],
    'chiral_tag': [0, 1, 2, 3],
    'implicit_valence': [0, 1, 2, 3, 4, 5, 6],
    'num_Hs': [0, 1, 2, 3, 4],
    'hybridization': [
        Chem.rdchem.HybridizationType.SP,
        Chem.rdchem.HybridizationType.SP2,
        Chem.rdchem.HybridizationType.SP3,
        Chem.rdchem.HybridizationType.SP3D,
        Chem.rdchem.HybridizationType.SP3D2
    ],
    'num_radical_electrons': [0, 1, 2]
}

# len(choices) + 1 to include room for uncommon values; + 1 at end for IsAromatic
ATOM_FDIM = sum(len(choices) + 1 for choices in ATOM_FEATURES.values()) + 1

BOND_FDIM = 14

# Memoization
SMILES_TO_FEATURES = {}


def get_atom_fdim() -> int:
    """
    Gets the dimensionality of atom features.

    :return: The number of atom features.
    """
    return ATOM_FDIM


def get_bond_fdim(three_d: bool = False, virtual_edges: bool = False) -> int:
    """
    Gets the dimensionality of bond features.

    :param three_d: Whether to include 3D distance between atoms in bond features.
    :param virtual_edges: Whether to include virtual edges between non-bonded atoms.
    :return: The number of bond features.
    """
    return BOND_FDIM + three_d + (11 * virtual_edges)


def onek_encoding_unk(value: int, choices: List[int]) -> List[int]:
    """
    Creates a one-hot encoding.

    :param value: The value for which the encoding should be one.
    :param choices: A list of possible values.
    :return: A one-hot encoding of the value in a list of length len(choices) + 1.
    If value is not in the list of choices, then the final element in the encoding is 1.
    """
    encoding = [0] * (len(choices) + 1)
    index = choices.index(value) if value in choices else -1
    encoding[index] = 1

    return encoding


def atom_features(atom: Chem.rdchem.Atom) -> torch.Tensor:
    """
    Builds a feature vector for an atom.

    :param atom: An RDKit atom.
    :return: A PyTorch tensor containing the atom features.
    """
    return torch.Tensor(
        onek_encoding_unk(atom.GetAtomicNum() - 1, ATOM_FEATURES['atomic_num'])
        + onek_encoding_unk(atom.GetDegree(), ATOM_FEATURES['degree'])
        + onek_encoding_unk(atom.GetFormalCharge(), ATOM_FEATURES['formal_charge'])
        + onek_encoding_unk(int(atom.GetChiralTag()), ATOM_FEATURES['chiral_tag'])
        + onek_encoding_unk(int(atom.GetImplicitValence()), ATOM_FEATURES['implicit_valence'])
        + onek_encoding_unk(int(atom.GetTotalNumHs()), ATOM_FEATURES['num_Hs'])
        + onek_encoding_unk(int(atom.GetHybridization()), ATOM_FEATURES['hybridization'])
        + onek_encoding_unk(int(atom.GetNumRadicalElectrons()), ATOM_FEATURES['num_radical_electrons'])
        + [atom.GetIsAromatic()]
    )


def bond_features(bond: Chem.rdchem.Bond, distance_path: int = None, distance_3d: float = None) -> torch.Tensor:
    """
    Builds a feature vector for a bond.

    :param bond: A RDKit bond.
    :param distance_path: The topological (path) distance between the atoms in the bond.
    Note: This is always 1 if the atoms are actually bonded and >1 for "virtual" edges between non-bonded atoms.
    :param distance_3d: The Euclidean distance between the atoms in 3D space.
    :return: A PyTorch tensor containing the bond features.
    """
    if bond is None:
        fbond = [1] + [0] * 13
    else:
        bt = bond.GetBondType()
        fbond = [
            0,  # bond is not None
            bt == Chem.rdchem.BondType.SINGLE,
            bt == Chem.rdchem.BondType.DOUBLE,
            bt == Chem.rdchem.BondType.TRIPLE,
            bt == Chem.rdchem.BondType.AROMATIC,
            (bond.GetIsConjugated() if bt is not None else 0),
            (bond.IsInRing() if bt is not None else 0)
        ]
        fbond += onek_encoding_unk(int(bond.GetStereo()), list(range(6)))

    fdistance_path = onek_encoding_unk(distance_path, list(range(10))) if distance_path is not None else []
    fdistance_3d = [distance_3d] if distance_3d is not None else []

    return torch.Tensor(fbond + fdistance_path + fdistance_3d)


def mol2graph(mol_batch: List[str],
              addHs: bool = False,
              three_d: bool = False,
              virtual_edges: bool = False) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, List[Tuple[int, int]]]:
    """
    Converts a list of SMILES strings to a batch of molecular graphs consisting of of PyTorch tensors.

    :param mol_batch: A list of SMILES strings.
    :param addHs: Whether to include Hydrogen atoms in each molecular graph.
    :param three_d: Whether to include 3D information in atom and bond features.
    :param virtual_edges: Whether to include virtual edges between non-bonded atoms.
    :return: A tuple of tensors representing a batch of molecular graphs. TODO: Better explanation.
    """
    padding = torch.zeros(get_atom_fdim() + get_bond_fdim(three_d=three_d, virtual_edges=virtual_edges))
    fatoms, fbonds = [], [padding]  # Ensure bond is 1-indexed
    in_bonds, all_bonds = [], [(-1, -1)]  # Ensure bond is 1-indexed
    scope = []
    total_atoms = 0

    for smiles in mol_batch:
        if smiles in SMILES_TO_FEATURES:
            mol_fatoms, mol_fbonds, mol_all_bonds, n_atoms = SMILES_TO_FEATURES[smiles]
        else:
            mol_fatoms, mol_fbonds, mol_all_bonds = [], [], []

            # Convert smiles to molecule
            mol = Chem.MolFromSmiles(smiles)

            if addHs:
                mol = Chem.AddHs(mol)

            # Get 3D distance matrix
            if three_d:
                mol = Chem.AddHs(mol)
                AllChem.EmbedMolecule(mol, AllChem.ETKDG())
                if not addHs:
                    mol = Chem.RemoveHs(mol)
                distances_3d = Chem.Get3DDistanceMatrix(mol)

            # Get topological (i.e. path-length) distance matrix and number of atoms
            distances_path = Chem.GetDistanceMatrix(mol)
            n_atoms = mol.GetNumAtoms()

            # Get atom features
            for atom in mol.GetAtoms():
                mol_fatoms.append(atom_features(atom))

            # Get bond features
            for a1 in range(n_atoms):
                for a2 in range(a1 + 1, n_atoms):
                    bond = mol.GetBondBetweenAtoms(a1, a2)

                    if bond is None and not virtual_edges:
                        continue

                    distance_3d = distances_3d[a1, a2] if three_d else None
                    distance_path = distances_path[a1, a2] if virtual_edges else None

                    mol_all_bonds.append((a1, a2))
                    mol_fbonds.append(torch.cat([mol_fatoms[a1], bond_features(bond,
                                                                               distance_path=distance_path,
                                                                               distance_3d=distance_3d)], dim=0))

                    mol_all_bonds.append((a2, a1))
                    mol_fbonds.append(torch.cat([mol_fatoms[a2], bond_features(bond,
                                                                               distance_path=distance_path,
                                                                               distance_3d=distance_3d)], dim=0))

            # Memoize
            SMILES_TO_FEATURES[smiles] = (mol_fatoms, mol_fbonds, mol_all_bonds, n_atoms)

        # Add molecule features to batch features
        fatoms.extend(mol_fatoms)
        fbonds.extend(mol_fbonds)

        in_bonds.extend([[] for _ in range(n_atoms)])

        for a1, a2 in mol_all_bonds:
            a1 += total_atoms
            a2 += total_atoms

            bond_idx = len(all_bonds)
            all_bonds.append((a1, a2))
            in_bonds[a2].append(bond_idx)

        scope.append((total_atoms, n_atoms))
        total_atoms += n_atoms

    total_bonds = len(all_bonds)
    max_num_bonds = max(len(bonds) for bonds in in_bonds)

    fatoms = torch.stack(fatoms, dim=0)
    fbonds = torch.stack(fbonds, dim=0)
    agraph = torch.zeros(total_atoms, max_num_bonds, dtype=torch.long)
    bgraph = torch.zeros(total_bonds, max_num_bonds, dtype=torch.long)

    for a in range(total_atoms):
        for i, b in enumerate(in_bonds[a]):
            agraph[a, i] = b

    for b1 in range(1, total_bonds):
        x, y = all_bonds[b1]
        for i, b2 in enumerate(in_bonds[x]):
            if all_bonds[b2][0] != y:
                bgraph[b1, i] = b2

    return fatoms, fbonds, agraph, bgraph, scope


def build_MPN(args, num_tasks) -> nn.Module:
    """
    Builds a message passing neural network including final linear layers and initializes parameters.

    :param hidden_size: Dimensionality of hidden layers.
    :param depth: Number of message passing steps.
    :param num_tasks: Number of tasks to predict.
    :param sigmoid: Whether to add a sigmoid layer at the end for classification.
    :param dropout: Dropout probability.
    :param activation: Activation function.
    :param attention: Whether to perform self attention over the atoms in a molecule.
    :param three_d: Whether to include 3D information in atom and bond features.
    :param virtual_edges: Whether to include virtual edges between non-bonded atoms.
    :return: An nn.Module containing the MPN encoder along with final linear layers with parameters initialized.
    """
    encoder = MPN(args)
    modules = [
        encoder,
        nn.Linear(args.hidden_size, args.hidden_size),
        nn.ReLU(),
        nn.Linear(args.hidden_size, num_tasks)
    ]
    if args.dataset_type == 'classification':
        modules.append(nn.Sigmoid())

    model = nn.Sequential(*modules)

    for param in model.parameters():
        if param.dim() == 1:
            nn.init.constant_(param, 0)
        else:
            nn.init.xavier_normal_(param)

    return model


class MPN(nn.Module):
    """A message passing neural network for encoding a molecule."""

    def __init__(self, args):
        """
        Initializes the MPN.

        :param hidden_size: Hidden dimensionality.
        :param depth: Number of message passing steps.
        :param dropout: Dropout probability.
        :param activation: Activation function.
        :param attention: Whether to perform self attention over the atoms in a molecule.
        :param message_attention: Whether to perform attention over incoming messages. 
        :param three_d: Whether to include 3D information in atom and bond features.
        :param virtual_edges: Whether to include virtual edges between non-bonded atoms.
        """
        super(MPN, self).__init__()
        self.hidden_size = args.hidden_size
        self.depth = args.depth
        self.dropout = args.dropout
        self.attention = args.attention
        self.message_attention = args.message_attention

        self.dropout_layer = nn.Dropout(p=self.dropout)
        self.W_i = nn.Linear(get_atom_fdim() + get_bond_fdim(three_d=args.three_d, virtual_edges=args.virtual_edges),
                             args.hidden_size, bias=False)
        self.W_h = nn.Linear(args.hidden_size, args.hidden_size, bias=False)
        self.W_o = nn.Linear(get_atom_fdim() + args.hidden_size, args.hidden_size)
        if self.attention:
            self.W_a = nn.Linear(args.hidden_size, args.hidden_size, bias=False)
            self.W_b = nn.Linear(args.hidden_size, args.hidden_size)
        if self.message_attention:
            self.W_ma1 = nn.Linear(args.hidden_size, 1, bias=False)
            # uncomment this later if you want attention over binput + nei_message? or on atom incoming at end
            # self.W_ma2 = nn.Linear(hidden_size, 1, bias=False)

        if args.activation == "ReLU":
            self.act_func = nn.ReLU()
        elif args.activation == "LeakyReLU":
            self.act_func = nn.LeakyReLU(0.1)
        elif args.activation == "PReLU":
            self.act_func = nn.PReLU()
        elif args.activation == 'tanh':
            self.act_func = nn.Tanh()
        else:
            raise ValueError('Activation "{}" not supported.'.format(args.activation))

    def forward(self, mol_graph: Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, List[Tuple[int, int]]]) -> torch.Tensor:
        """
        Encodes a batch of molecular graphs.

        :param mol_graph: A tuple containing a batch of molecular graphs.
        :return: A PyTorch tensor containing the encoding of the graph.
        """
        fatoms, fbonds, agraph, bgraph, scope = mol_graph
        if next(self.parameters()).is_cuda:
            fatoms, fbonds, agraph, bgraph = fatoms.cuda(), fbonds.cuda(), agraph.cuda(), bgraph.cuda()

        binput = self.W_i(fbonds)
        message = self.act_func(binput)

        for i in range(self.depth - 1):
            nei_message = index_select_ND(message, 0, bgraph)
            if self.message_attention:
                nei_message = nei_message * torch.softmax(self.W_ma1(nei_message), dim=1).repeat((1, 1, self.hidden_size)) #num_bonds x num_messages x 1
            nei_message = nei_message.sum(dim=1) # num_bonds x num_messages x hidden
            nei_message = self.W_h(nei_message)
            message = self.act_func(binput + nei_message)
            message = self.dropout_layer(message)

        nei_message = index_select_ND(message, 0, agraph)
        nei_message = nei_message.sum(dim=1)
        ainput = torch.cat([fatoms, nei_message], dim=1)
        atom_hiddens = self.act_func(self.W_o(ainput))
        atom_hiddens = self.dropout_layer(atom_hiddens)

        mol_vecs = []
        for st, le in scope:
            cur_hiddens = atom_hiddens.narrow(0, st, le)

            if self.attention:
                att_w = torch.matmul(self.W_a(cur_hiddens), cur_hiddens.t())
                att_w = F.softmax(att_w, dim=1)
                att_hiddens = torch.matmul(att_w, cur_hiddens)
                att_hiddens = self.act_func(self.W_b(att_hiddens))
                att_hiddens = self.dropout_layer(att_hiddens)
                mol_vec = (cur_hiddens + att_hiddens)
            else:
                mol_vec = cur_hiddens

            mol_vec = mol_vec.sum(dim=0) / le
            mol_vecs.append(mol_vec)

        mol_vecs = torch.stack(mol_vecs, dim=0)
        return mol_vecs
