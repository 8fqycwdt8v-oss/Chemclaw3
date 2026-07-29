"""pH-dependent distribution coefficient (logD) — research follow-up, D-092.

Analytical method development (HPLC mobile-phase pH, liquid-liquid extraction, formulation)
routinely needs logD at a working pH, not just the pH-independent logP the solubility model
already computes internally. This composes two calculators that already exist rather than adding
a new model or a new cache entry: Crippen LogP (`rdkit.Chem.Crippen`, the same descriptor
`chemclaw.science.calc.solubility` uses) and the cached GFN2-xTB pKa predictor
(`chemclaw.science.calc.pka`), combined via the
standard Henderson-Hasselbalch relation. No new dependency, no new science, and no double
caching — the expensive half (the xTB pKa) is already memoized by `run_cached_pka`; Crippen LogP
is a sub-millisecond descriptor, so wrapping the composition in its own store entry would add a
second cache for no benefit.

**Domain, inherited wholesale from `chemclaw.science.calc.pka`.** Neutral O-H/S-H **acids**
(carboxylic acids,
phenols, alcohols, thiols) and the conjugate acid of **aromatic or aryl nitrogen** (pyridines,
azoles, anilines). Aliphatic amines and molecules with neither site raise —
`chemclaw.science.calc.pka`'s own
`ValueError` propagates unchanged (gate G4).

That domain widened underneath this module in X11, and the widening had teeth: `predict_pka`
began returning bases where it previously raised, and the Henderson-Hasselbalch correction runs
in the *opposite direction* for one. `PkaResult.site` is what makes the two distinguishable, and
`predict_logd` branches on it. Depending on a collaborator's domain is fine; depending on it
without reading which half of the domain you were handed is what produced a two-log-unit error
that raised nothing.
"""

import math

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Crippen

from chemclaw.core.config import settings
from chemclaw.science.calc.pka import PkaInput, run_cached_pka
from chemclaw.science.calc.store import ResultStore


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

    Raises `ValueError` exactly as `chemclaw.science.calc.pka.predict_pka` does: on an unparseable
    SMILES, a
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
    # Henderson-Hasselbalch, and the sign of this exponent is the entire content of it.
    #   acid  HA  <-> A- + H+ : the ionized fraction *rises* with pH  -> 10**(pH - pKa)
    #   base  BH+ <-> B  + H+ : the ionized fraction *falls* with pH  -> 10**(pKa - pH)
    # Written as a branch on `site` rather than one formula because getting it wrong is
    # silent: before this, a base took the acid form and pyridine at pH 7.4 came out two
    # log units too lipophobic while looking entirely ordinary. `calc.pka` only began
    # returning bases in X11, so the acid-only formula was correct when it was written and
    # became wrong when the predictor's domain widened underneath it.
    exponent = ph - pka_result.pka if pka_result.site == "acid" else pka_result.pka - ph
    log_d = clogp - math.log10(1 + 10**exponent)
    return LogdResult(
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=log_d,
        uncertainty=pka_result.uncertainty,
    )
