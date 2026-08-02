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

**Domain, inherited from `chemclaw.science.calc.pka` and then narrowed once.** Neutral O-H/S-H
**acids** (carboxylic acids,
phenols, alcohols, thiols) and the conjugate acid of **aromatic or aryl nitrogen** (pyridines,
azoles, anilines). Aliphatic amines and molecules with neither site raise —
`chemclaw.science.calc.pka`'s own
`ValueError` propagates unchanged (gate G4).

The narrowing is this module's own, because the composition is where it bites: `predict_pka`
reports **one** pKa and one number is all a single Henderson-Hasselbalch term can consume, so a
molecule ionising at two sites is outside what this arithmetic can express even though both
halves it composes are inside theirs. `_require_a_single_equilibrium` refuses those — polyprotic
acids, and amphoterics, which had been slipping past `predict_pka`'s aliphatic-amine refusal
because a carboxyl sends the molecule down the acid branch before the amine is ever looked at.

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
from chemclaw.science.calc.pka import PkaInput, PkaResult, ionisable_sites, run_cached_pka
from chemclaw.science.calc.store import ResultStore
from chemclaw.science.calc.uncertainty import CalculationDomainError


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


def _require_a_single_equilibrium(result: PkaResult, ph: float, ionised_ratio: float) -> None:
    """Raise unless one Henderson-Hasselbalch term can describe this whole molecule.

    `predict_pka` reports one pKa and this module applies one ionisation term, so a molecule
    with a second ionisable site is only served correctly when that site is spectator. Two
    situations where it is not, and neither is recoverable from the surface `calc.pka` offers —
    a second pKa is simply not computed — so both refuse rather than return a number:

    - **Amphoteric** (an acid site *and* a base site). Refused at every pH. `predict_pka` takes
      the acid branch whenever any O-H/S-H is present, so the base site is never even evaluated
      and nothing bounds its ionisation. Glycine at pH 7.4 is the measured case: it returned
      -2.81 with no error, silently evading the very refusal `predict_pka` raises for piperidine,
      because a carboxyl kept it out of the aliphatic-amine branch it belonged in.
    - **Polyprotic** (two or more sites of the same kind) *while the reported site is
      substantially ionised*. The reported site is the most ionisable one, so when it is
      essentially neutral every other site is even more so and the single term is exact to within
      `settings.logd_negligible_ionised_fraction`'s bound — which is what keeps a diol or a sugar
      (O-H sites with pKa ~15) working. Above that threshold the unseen equilibrium is unbounded:
      succinic acid at pH 7.4 returned -1.48 +/- 1.6 against a true value near -5, one carboxyl
      accounted for and one ignored.

    **Refusal rather than an out-of-domain `Estimate`**, though both conventions exist in the
    tree (`calc.solubility` flags). Two reasons. This module has only ever had the first: its
    domain limits are `ValueError`s inherited from `calc.pka`, and the aliphatic-amine case this
    closes is *already* a refusal, so flagging would make one hazard visible in a field and its
    twin visible in an exception. And the two are not the same kind of claim — ESOL on a salt
    returns a number of unknown validity, whereas this returns one known to be wrong by 2-5 log
    units, which is a number no caller should be handed at all.
    """
    sites = ionisable_sites(result.smiles)
    if sites.acidic and sites.basic:
        raise CalculationDomainError(
            f"{result.smiles!r} is amphoteric ({sites.acidic} acidic O-H/S-H site(s) and "
            f"{sites.basic} basic nitrogen(s)): its acid and base equilibria run in opposite "
            "directions and this calculator applies one ionisation term to the single pKa "
            "`calc.pka` reports — which for an amphoteric molecule is always the acid site, so "
            "the base site is neither computed nor bounded. No logD rather than a plausible one"
        )
    if sites.total < 2:
        return
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    if ionised_fraction > settings.logd_negligible_ionised_fraction:
        kind = "acidic O-H/S-H site(s)" if result.site == "acid" else "basic nitrogen(s)"
        raise CalculationDomainError(
            f"{result.smiles!r} has {sites.total} {kind} and is {ionised_fraction:.0%} ionised at "
            f"pH {ph:g} on the one site `calc.pka` reports (pKa {result.pka:.2f}). A second "
            "ionisation of comparable size is unaccounted for and its pKa is not computable from "
            "this predictor, so the single-equilibrium logD would be wrong by an unbounded "
            "amount (measured: succinic acid at pH 7.4 gives -1.5 against a true value near -5)"
        )


async def predict_logd(store: ResultStore, job: LogdInput) -> LogdResult:
    """Predict logD at `job.ph` (or the configured default) for a singly-ionisable molecule.

    Raises `ValueError` everywhere `chemclaw.science.calc.pka.predict_pka` does — an unparseable
    SMILES, a net-charged or open-shell input, a molecule with no acidic O-H/S-H site, an
    aliphatic amine — and additionally where a *single* Henderson-Hasselbalch term cannot
    describe the molecule at this pH (see `_require_a_single_equilibrium`). Never a guessed logD
    (gate G4). The pKa half is served from the calculation store on a repeat, so re-asking at a
    different pH for the same molecule costs only the trivial LogP recompute.
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
    # [ionized]/[neutral] — the same quantity the correction and the domain check both need,
    # computed once so the number that is refused on is the number that would have been used.
    ionised_ratio = 10.0**exponent
    _require_a_single_equilibrium(pka_result, ph, ionised_ratio)
    log_d = clogp - math.log10(1.0 + ionised_ratio)
    return LogdResult(
        smiles=pka_result.smiles,
        ph=ph,
        clogp=clogp,
        pka=pka_result.pka,
        log_d=log_d,
        uncertainty=pka_result.uncertainty,
    )
