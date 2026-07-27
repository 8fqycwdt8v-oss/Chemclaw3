"""pH-dependent distribution coefficient (logD) — research follow-up, D-092.

Analytical method development (HPLC mobile-phase pH, liquid-liquid extraction, formulation)
routinely needs logD at a working pH, not just the pH-independent logP the solubility model
already computes internally. This composes two calculators that already exist rather than adding
a new model or a new cache entry: Crippen LogP (`rdkit.Chem.Crippen`, the same descriptor
`calc.solubility` uses) and the cached GFN2-xTB pKa predictor (`calc.pka`), combined via the
standard Henderson-Hasselbalch relation. No new dependency, no new science, and no double
caching — the expensive half (the xTB pKa) is already memoized by `run_cached_pka`; Crippen LogP
is a sub-millisecond descriptor, so wrapping the composition in its own store entry would add a
second cache for no benefit.

Domain: `calc.pka` only covers neutral O-H/S-H **acids** (carboxylic acids, phenols, alcohols,
thiols); logD here inherits that restriction and raises rather than silently mishandling a base
or a molecule with no acidic site (gate G4) — `calc.pka`'s own `ValueError` propagates unchanged.
A logD for basic (amine) sites follows once `calc.pka` itself covers N-H/C-H acids (noted there
as a later extension).
"""

import math

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Crippen

from calc.pka import PkaInput, run_cached_pka
from calc.store import ResultStore
from chemclaw.config import settings


class LogdInput(BaseModel):
    """A logD request: the molecule and the pH (defaults to `settings.logd_default_ph`)."""

    smiles: str = Field(min_length=1)
    ph: float | None = None


class LogdResult(BaseModel):
    """Predicted logD at a given pH, alongside the logP/pKa it was derived from.

    `uncertainty` propagates only the pKa calibration's residual (the dominant error term);
    Crippen LogP itself carries no reported uncertainty in RDKit.
    """

    smiles: str
    ph: float
    clogp: float
    pka: float
    log_d: float
    uncertainty: float


async def predict_logd(store: ResultStore, job: LogdInput) -> LogdResult:
    """Predict logD at `job.ph` (or the configured default) for a neutral O-H/S-H acid.

    Raises `ValueError` exactly as `calc.pka.predict_pka` does: on an unparseable SMILES, a
    net-charged or open-shell input, or a molecule with no acidic O-H/S-H site — never a guessed
    logD (gate G4). The pKa half is served from the calculation store on a repeat, so re-asking
    at a different pH for the same molecule costs only the trivial LogP recompute.
    """
    ph = settings.logd_default_ph if job.ph is None else job.ph
    pka_result, _ = await run_cached_pka(store, PkaInput(smiles=job.smiles))
    # `pka_result.smiles` is already the canonical form `run_cached_pka` computed on, so this
    # reparse cannot fail — the acid was already proven parseable to get here.
    mol = Chem.MolFromSmiles(pka_result.smiles)
    assert mol is not None  # pragma: no cover - guaranteed by run_cached_pka's own validation
    clogp = Crippen.MolLogP(mol)
    log_d = clogp - math.log10(1 + 10 ** (ph - pka_result.pka))
    return LogdResult(
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=log_d,
        uncertainty=pka_result.uncertainty,
    )
