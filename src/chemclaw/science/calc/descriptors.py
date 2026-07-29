"""Developability descriptor panel (research follow-up, D-092).

Fills a real gap identified while surveying open-source cheminformatics for process-development
tools: the only physicochemical descriptors exposed today are the four buried inside the ESOL
solubility model (`chemclaw.science.calc.solubility`). Chemists routinely need the panel itself —
molecular weight,
lipophilicity, polar surface area, H-bond counts, rotatable bonds, sp3 fraction, QED — to screen a
candidate's developability (Lipinski Rule-of-Five, Veber's oral-bioavailability rule) before
committing bench time. Every descriptor here is a closed-form RDKit computation (the same library
every other calculator already depends on), so this ships with **zero new dependencies** and no
license/offline risk, unlike a trained model.
"""

from importlib.metadata import version

from pydantic import BaseModel
from rdkit import Chem
from rdkit.Chem import QED, Crippen, Descriptors, rdMolDescriptors

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.science.calc.store import CalculationKey, ResultStore, run_cached

CALC_TYPE = "developability"


class DescriptorInput(BaseModel):
    """A descriptor-panel request: just the molecule."""

    smiles: str


class DescriptorProfile(BaseModel):
    """The developability descriptor panel for one molecule, plus rule-of-thumb flags.

    `lipinski_violations` counts the four Rule-of-Five criteria (MW>500, LogP>5, HBD>5, HBA>10)
    the molecule breaks; `veber_pass` is Veber's oral-bioavailability heuristic (rotatable bonds
    <=10 and TPSA<=140 A^2). Both are widely used triage heuristics, not developability verdicts —
    report them as flags a chemist weighs, never as a pass/fail gate on their own.
    """

    smiles: str
    molecular_weight: float
    clogp: float
    tpsa: float
    h_bond_donors: int
    h_bond_acceptors: int
    rotatable_bonds: int
    aromatic_rings: int
    fraction_csp3: float
    qed: float
    lipinski_violations: int
    veber_pass: bool


def compute_descriptor_profile(job: DescriptorInput) -> DescriptorProfile:
    """Compute the developability descriptor panel for one molecule.

    Raises `ValueError` on an unparseable SMILES rather than returning a bogus panel (gate G4).
    """
    mol = Chem.MolFromSmiles(job.smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {job.smiles!r}")

    mw = Descriptors.MolWt(mol)
    clogp = Crippen.MolLogP(mol)
    tpsa = rdMolDescriptors.CalcTPSA(mol)
    hbd = rdMolDescriptors.CalcNumHBD(mol)
    hba = rdMolDescriptors.CalcNumHBA(mol)
    rotatable = rdMolDescriptors.CalcNumRotatableBonds(mol)
    aromatic_rings = rdMolDescriptors.CalcNumAromaticRings(mol)
    fraction_csp3 = rdMolDescriptors.CalcFractionCSP3(mol)
    qed = QED.qed(mol)

    violations = sum([mw > 500, clogp > 5, hbd > 5, hba > 10])
    veber_pass = rotatable <= 10 and tpsa <= 140

    return DescriptorProfile(
        smiles=job.smiles,
        molecular_weight=mw,
        clogp=clogp,
        tpsa=tpsa,
        h_bond_donors=hbd,
        h_bond_acceptors=hba,
        rotatable_bonds=rotatable,
        aromatic_rings=aromatic_rings,
        fraction_csp3=fraction_csp3,
        qed=qed,
        lipinski_violations=violations,
        veber_pass=veber_pass,
    )


def _calc_version() -> str:
    """Cache-key version tying the panel to the RDKit build (D-011).

    Every descriptor here is a pure RDKit computation, so an RDKit upgrade is the only thing that
    can shift a value; keying on its version is enough (mirrors `chemclaw.science.calc.solubility`).
    """
    return f"rdkit-{version('rdkit')}"


async def run_cached_descriptor_profile(
    store: ResultStore, job: DescriptorInput
) -> tuple[DescriptorProfile, bool]:
    """Return a descriptor panel for `job`, reusing the store on a repeat.

    Keyed on the canonical SMILES so two spellings of the same molecule share one cache entry
    (D-011 determinism), same discipline as every other calculator in `calc/`.
    """
    canonical = require_canonical_smiles(job.smiles)
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=_calc_version(),
        inputs={"smiles": canonical},
    )
    return await run_cached(
        store,
        key,
        lambda: compute_descriptor_profile(DescriptorInput(smiles=canonical)),
        DescriptorProfile,
    )
