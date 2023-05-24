#%%
import numpy as np
import dgl
import torch
import tempfile
import os.path
from PDBData.PDBMolecule import PDBMolecule
from pathlib import Path
from typing import Union, List, Tuple, Dict, Any, Optional
import h5py
from PDBData.units import DISTANCE_UNIT, ENERGY_UNIT, FORCE_UNIT
from openmm.unit import bohr, Quantity, hartree, mole
from openmm.unit import unit
from openmm.app import ForceField
#%%

class PDBDataset:
    """
    Handles the generation of dgl graphs from PDBMolecules. Stores PDBMolecules in a list.
    Uses an hdf5 file containing the pdbfiles as string and xyz, elements, energies and gradients as numpy arrays to store tha dataset on a hard drive.
    """
    def __init__(self)->None:
        self.mols = []
        self.info = True

    def __len__(self)->int:
        return len(self.mols)

    def __getitem__(self, idx:int)->dgl.graph:
        return self.mols[idx]

    def append(self, mol:PDBMolecule)->None:
        """
        Appends a PDBMolecule to the dataset.
        """
        self.mols.append(mol)
    
    def to_dgl(self, idxs:List[int]=None, split:List[float]=None, seed:int=0)->List[dgl.graph]:
        """
        Converts a list of indices to a list of dgl graphs.
        """
        if idxs is None:
            idxs = list(range(len(self)))
        if self.info:
            print("converting PDBDataset to dgl graphs...")
        def _get_graph(i, idx):
            if self.info:
                print(f"converting {i+1}/{len(idxs)}", end="\r")
            return self.mols[idx].to_dgl(graph_data=True)
        if self.info:
            print()
        
        glist = [_get_graph(i, idx) for i, idx in enumerate(idxs)]
        if split is None:
            return glist
        else:
            return dgl.data.utils.split_dataset(glist, split, shuffle=True, random_state=seed)
    
    def save(path:Union[str, Path]):
        """
        Saves the dataset to an hdf5 file.
        """
        pass

    def save_npz(self, path:Union[str, Path], overwrite:bool=False):
        """
        Save the dataset to npz files.
        """
        if os.path.exists(str(path)) and not overwrite:
            raise FileExistsError(f"path {str(path)} already exists, set overwrite=True to overwrite it.")
        
        os.makedirs(path, exist_ok=True)
        for id, mol in enumerate(self.mols):
            mol.compress(str(Path(path)/Path(str(id)+".npz")))
    
    @classmethod
    def load(cls, path:Union[str, Path]):
        """
        Loads a dataset from an hdf5 file.
        """
        obj = cls()
        # load here
        return obj

    @classmethod
    def load_npz(cls, path:Union[str, Path], keep_order=False):
        """
        Loads a dataset from npz files.
        """
        obj = cls()
        # load:
        if not keep_order:
            paths = Path(path).rglob('*.npz')
        else:
            paths = sorted([p for p in Path(path).rglob('*.npz')])
        for npz in paths:
            mol = PDBMolecule.load(Path(npz))
            obj.append(mol)
        return obj

    def save_dgl(self, path:Union[str, Path], idxs:List[int]=None):
        """
        Saves the dgl graphs that belong to the dataset.
        """
        dgl.save_graphs(path, self.to_dgl(idxs))

    
    def parametrize(self, forcefield:ForceField=ForceField('amber99sbildn.xml'), suffix:str="_amber99sbildn", get_charges=None)->None:
        """
        Parametrizes the dataset with a forcefield.
        Writes the following entries to the graph:
        ...
        get_charges: a function that takes a topology and returns a list of charges as openmm Quantities in the order of the atoms in topology.
        """
        if self.info:
            print("parametrizing PDBDataset...")
        for i, mol in enumerate(self.mols):
            if self.info:
                print(f"parametrizing {i+1}/{len(self.mols)}", end="\r")
            mol.parametrize(forcefield=forcefield, suffix=suffix, get_charges=get_charges)
        if self.info:
            print()

    
    def filter_validity(self, forcefield:ForceField=ForceField("amber99sbildn.xml"), sigmas:Tuple[float,float]=(1.,1.))->None:
        """
        Checks if the stored energies and gradients are consistent with the forcefield. Removes the entry from the dataset if the energies and gradients are not within var_param[0] and var_param[1] standard deviations of the stored energies/forces. If sigmas are 1, this corresponds to demanding that the forcefield data is better than simply always guessing the mean.
        """
        if self.info:
            print("filtering valid mols of PDBDataset by comparing with class ff...")
        keep = []
        removed = 0
        for i, mol in enumerate(self.mols):
            valid = mol.conf_check(forcefield=forcefield, sigmas=sigmas)
            keep.append(valid)
            removed += int(not valid)
            if self.info:
                print(f"filtering {i+1}/{len(self.mols)}, kept {len(self)}, removed {removed}", end="\r")
        if self.info:
            print()

        # use slicing to modify the list inplace:
        self.mols[:] = [mol for i, mol in enumerate(self.mols) if keep[i]]
        

        
    def filter_confs(self, max_energy:float=60., max_force:float=None)->None:
        """
        Filters out conformations with energies or forces that are over 60 kcal/mol away from the minimum of the dataset (not the actual minimum). Remove molecules is less than 2 conformations are left. Apply this before parametrizing or re-apply the parametrization after filtering. Units are kcal/mol and kcal/mol/angstrom.
        """

        keep = []
        for i, mol in enumerate(self.mols):
            more_than2left = mol.filter_confs(max_energy=max_energy, max_force=max_force)
            keep.append(more_than2left)

        # use slicing to modify the list inplace:
        self.mols[:] = [mol for i, mol in enumerate(self.mols) if keep[i]]


    @classmethod
    def from_hdf5(
        cls,
        path: Union[str,Path],
        element_key: str = "atomic_numbers",
        energy_key: str = "dft_total_energy",
        xyz_key: str = "conformations",
        grad_key: str = "dft_total_gradient",
        hdf5_distance: unit = DISTANCE_UNIT,
        hdf5_energy: unit = ENERGY_UNIT,
        hdf5_force: unit = FORCE_UNIT,
        n_max:int=None,
        skip_errs:bool=True,
        info:bool=True,):
        """
        Generates a dataset from an hdf5 file.
        """
        obj = cls()
        obj.info = info
        counter = 0
        failed_counter = 0
        if info:
            print("loading dataset from hdf5 file...") 
        with h5py.File(path, "r") as f:
            for name in f.keys():
                if not n_max is None:
                    if len(obj) > n_max:
                        break
                try:
                    elements = f[name][element_key]
                    energies = f[name][energy_key]
                    xyz = f[name][xyz_key]
                    grads = f[name][grad_key]
                    elements = np.array(elements, dtype=np.int64)
                    xyz = Quantity(np.array(xyz), hdf5_distance).value_in_unit(DISTANCE_UNIT)
                    grads = Quantity(np.array(grads), hdf5_force).value_in_unit(FORCE_UNIT)
                    energies = Quantity(np.array(energies) - np.array(energies).mean(axis=-1), hdf5_energy).value_in_unit(ENERGY_UNIT)

                    mol = PDBMolecule.from_xyz(elements=elements, xyz=xyz, energies=energies, gradients=grads)
                    obj.append(mol)
                    counter += 1
                except:
                    failed_counter += 1
                    if not skip_errs:
                        raise
                if info:
                    print(f"stored {counter}, failed for {failed_counter}, storing {str(name)[:8]} ...", end="\r")

        if info:
            print()
        return obj


    @classmethod
    def from_spice(cls, path: Union[str,Path], info:bool=True):
        """
        Generates a dataset from an hdf5 file with spice unit convention.
        """
        PARTICLE = mole.create_unit(6.02214076e23 ** -1, "particle", "particle")
        HARTREE_PER_PARTICLE = hartree / PARTICLE
        SPICE_DISTANCE = bohr
        SPICE_ENERGY = HARTREE_PER_PARTICLE
        SPICE_FORCE = SPICE_ENERGY / SPICE_DISTANCE

        return cls.from_hdf5(
            path,
            element_key="atomic_numbers",
            energy_key="dft_total_energy",
            xyz_key="conformations",
            grad_key="dft_total_gradient",
            hdf5_distance=SPICE_DISTANCE,
            hdf5_energy=SPICE_ENERGY,
            hdf5_force=SPICE_FORCE,
            info=info
        )


#%%
if __name__ == "__main__":
    spicepath = Path(__file__).parent.parent.parent / Path("mains/small_spice")
    dspath = Path(spicepath)/Path("small_spice.hdf5")
    ds = PDBDataset.from_spice(dspath)
    # %%
    ds.filter_validity()
    len(ds)
    # %%
    ds.filter_confs()
    len(ds)
    #%%
    # check saving and loading:

    with tempfile.TemporaryDirectory() as tmpdirname:
        ds.save_npz(tmpdirname)
        ds2 = PDBDataset.load_npz(tmpdirname)

    assert len(ds) == len(ds2), f"lengths are not the same: {len(ds)} vs {len(ds2)}"
    
    assert set([mol.xyz.shape for mol in ds.mols]) == set([mol.xyz.shape for mol in ds2.mols]), f"shapes of xyz arrays are not the same: \n{set([mol.xyz.shape for mol in ds.mols])} \nvs \n{set([mol.xyz.shape for mol in ds2.mols])}"
    #%%
    ds.parametrize()
    # %%
    with tempfile.TemporaryDirectory() as tmpdirname:
        ds.save_npz(tmpdirname)
        ds2 = PDBDataset.load_npz(tmpdirname)
    glist = ds2.to_dgl()
    assert "u_total_amber99sbildn" in glist[0].nodes["g"].data.keys()
    # %%
    with tempfile.TemporaryDirectory() as tmpdirname:
        p = str(Path(tmpdirname)/Path("test_dgl.bin"))
        ds.save_dgl(p)
        glist, _ = dgl.load_graphs(p)
        assert np.allclose(glist[0].nodes["g"].data["u_qm"], ds.to_dgl([0])[0].nodes["g"].data["u_qm"])

# %%