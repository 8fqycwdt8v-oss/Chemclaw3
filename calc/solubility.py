"""Aqueous solubility predictor (plan step 1c.3).

Ships an open, reproducible **ESOL baseline** (Delaney 2004): a closed-form model
over four RDKit descriptors, so predictions are real and license-free today. Every
prediction carries the model's reported uncertainty — a fast property estimate is
never presented as exact. There is one model, so it is called directly; when a
second (e.g. a trained GNN) exists, reintroduce a selection seam (Rule of Three).
"""

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors

from calc.store import CalculationKey, ResultStore, run_cached
from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings

CALC_TYPE = "solubility"


class SolubilityInput(BaseModel):
    """A solubility request: just the molecule."""

    smiles: str = Field(min_length=1)


class SolubilityResult(BaseModel):
    """Predicted aqueous solubility as log S (mol/L), with an uncertainty.

    `uncertainty_log` is one standard deviation in log-S units — report it so a
    consumer never treats the point estimate as exact.
    """

    smiles: str
    model: str
    log_s_mol_per_l: float
    uncertainty_log: float


class EsolBaseline:
    """Delaney (2004) ESOL model — a closed form over four RDKit descriptors.

    log S = 0.16 − 0.63·clogP − 0.0062·MW + 0.066·(rotatable bonds) − 0.74·(aromatic
    proportion). The reported RMSE (`settings.solubility_rmse_log`, ≈ 0.75 log units)
    is used as a constant uncertainty. A transparent, license-free default until a
    trained model replaces it.
    """

    name = "esol-delaney"
    version = "2004"

    def predict(self, mol: Chem.Mol) -> tuple[float, float]:
        """Return (log S mol/L, uncertainty) from the ESOL descriptor equation."""
        clogp = Crippen.MolLogP(mol)
        mw = Descriptors.MolWt(mol)
        rotatable = rdMolDescriptors.CalcNumRotatableBonds(mol)
        heavy = mol.GetNumHeavyAtoms()
        aromatic_proportion = (
            sum(1 for atom in mol.GetAtoms() if atom.GetIsAromatic()) / heavy if heavy else 0.0
        )
        log_s = 0.16 - 0.63 * clogp - 0.0062 * mw + 0.066 * rotatable - 0.74 * aromatic_proportion
        return log_s, settings.solubility_rmse_log


# The single solubility model. Called directly (no selection seam until a second exists).
_MODEL = EsolBaseline()


def predict_solubility(job: SolubilityInput) -> SolubilityResult:
    """Predict aqueous solubility for one molecule.

    Raises `ValueError` on an unparseable SMILES rather than returning a bogus
    number (gate G4).
    """
    mol = Chem.MolFromSmiles(job.smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {job.smiles!r}")
    log_s, uncertainty = _MODEL.predict(mol)
    return SolubilityResult(
        smiles=job.smiles,
        model=f"{_MODEL.name}@{_MODEL.version}",
        log_s_mol_per_l=log_s,
        uncertainty_log=uncertainty,
    )


async def run_cached_solubility(
    store: ResultStore, job: SolubilityInput
) -> tuple[SolubilityResult, bool]:
    """Return a solubility prediction for `job`, reusing the store on a repeat.

    The key is versioned by model name+version *and* the reported RMSE, so bumping the
    model or re-tuning `solubility_rmse_log` recomputes rather than serving a prediction
    (or a stale uncertainty) from the old one.
    """
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=f"{_MODEL.name}@{_MODEL.version}/u-{settings.solubility_rmse_log}",
        inputs={"smiles": require_canonical_smiles(job.smiles)},
    )
    return await run_cached(store, key, lambda: predict_solubility(job), SolubilityResult)
