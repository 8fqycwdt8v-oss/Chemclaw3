"""One shape for "how much should this number be trusted", across every calculator.

Ticket F8-T1 asked for `value + uncertainty + in_domain + method`, and conformal prediction where
feasible. What existed was three calculators each carrying an uncertainty in its own field name
(`uncertainty_log`, `uncertainty`), no notion of an applicability domain anywhere in the tree, and
nothing saying *where a number came from* — so `predict_solubility` on an organometallic returned a
confident value with a training-set RMSE attached, and the `calculation-selection` skill had nothing
machine-readable to consult.

**Three questions, and they are genuinely different.**

*How wrong is this likely to be?* — `uncertainty`, and `method` says how it was obtained.
`reported` is the model's published error, a constant that knows nothing about *this* molecule.
`conformal` is a split-conformal interval over this deployment's own recorded residuals, which is
strictly better evidence when there is enough of it. `propagated` is an input's uncertainty carried
through arithmetic (`logd` does this from the pKa calibration). The distinction matters to a
reviewer: a constant RMSE is a claim about a paper's test set, and a conformal interval is a claim
about this system.

*Was this molecule the kind of thing the model can speak about at all?* — `in_domain`. Separate from
the uncertainty because an out-of-domain prediction's error bar is not merely larger, it is
**meaningless**: extrapolating a linear fit does not widen its residual distribution, it leaves the
distribution the residuals were drawn from.

*Do we know?* — `None`, and it is not the same as `False`. A calculator with no declared domain
reports "unknown", which is the honest answer and the one that keeps a reviewer's attention.

**On what a domain check may honestly assert here.** The statistical applicability domain of a
fitted model — a leverage or a descriptor-space distance — needs its training set, and this
repository ships none. Inventing bounds and labelling them "the training ranges" would be a
fabricated threshold in a GxP system, which is worse than no check. What *can* be asserted without
the training set is the model's **structural** domain: what its terms are defined over at all. ESOL
is a linear equation in Crippen logP over neutral single-component organic molecules; it is not that
a salt is far from the training data, it is that the equation's inputs do not describe a salt. Those
checks follow from the model's definition, are citable, and catch exactly the cases where a
confident number is most wrong. The statistical half stays open and is named in the backlog rather
than approximated.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem

# How an uncertainty was arrived at. Not decoration: it is the difference between "the paper that
# published this model measured this spread on its own test set" and "this deployment measured this
# spread on its own chemistry", and a reviewer weighs the two differently.
Method = Literal["reported", "conformal", "propagated", "none"]

# Elements ESOL-style organic property models are parameterised over. Not a chemistry opinion — the
# Crippen contributions and the aromatic-proportion term are defined for organic structures, and a
# molecule outside this set is one the equation has no terms for.
_ORGANIC_ELEMENTS = frozenset({"H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Br", "I"})


class Estimate(BaseModel):
    """A number, its uncertainty, where that uncertainty came from, and whether to trust it at all.

    Deliberately not a replacement for the calculators' own result models: those carry the domain
    fields a chemist reads (a pKa's site, a solubility's model id), and flattening them into one
    generic envelope would lose that. This is the *uniform* part, produced beside them, so a skill,
    a note writer or a retrieval excerpt has one shape to consult regardless of which calculator
    answered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    unit: str = Field(min_length=1)
    # None when nothing about this prediction's error is known. Distinct from 0.0, which would
    # claim the prediction is exact — the failure mode the calibration ledger's own docstring warns
    # about for coverage.
    uncertainty: float | None = None
    method: Method = "none"
    # None = this calculator declares no applicability domain, so the question is unanswered rather
    # than answered "yes". A consumer that treats None as True is the bug this field exists to make
    # visible.
    in_domain: bool | None = None
    # Why not, in a chemist's terms, one reason per failed check. Empty when in domain or unknown.
    domain_reasons: tuple[str, ...] = ()

    @property
    def trustworthy(self) -> bool:
        """Whether a consumer may use this number without a human looking at it first.

        Requires an affirmative domain answer, not merely the absence of a negative one: `None`
        means nobody checked, and "nobody checked" must not read as "fine".
        """
        return self.in_domain is True


def structural_domain(mol: Chem.Mol) -> tuple[bool, tuple[str, ...]]:
    """Whether a molecule is the kind of structure an organic property model is defined over.

    Three checks, each following from what such a model *is* rather than from where its training set
    happened to fall — so they are defensible without the training data this repository does not
    ship:

    - **One component.** A salt or a co-crystal is two species; a single-molecule descriptor
      equation has no term for the counter-ion, and the answer it returns describes neither.
    - **Neutral.** Crippen contributions and the aromatic-proportion term are parameterised for
      neutral species. A charged form's solubility is a different physical quantity, usually by
      orders of magnitude, which is exactly the size of error a 0.75-log RMSE hides.
    - **Organic elements only.** An organometallic or a boron cluster has no Crippen contribution to
      sum; RDKit returns a number regardless, assembled from whatever fragments it recognises.

    Returns `(in_domain, reasons)`; `reasons` is empty when in domain.
    """
    reasons: list[str] = []
    if len(Chem.GetMolFrags(mol)) > 1:
        reasons.append(
            "multi-component structure (salt, co-crystal or mixture); the model describes one "
            "molecule and has no term for the counter-ion"
        )
    charge = Chem.GetFormalCharge(mol)
    if charge != 0:
        reasons.append(
            f"net formal charge {charge:+d}; the descriptors are parameterised for neutral species "
            "and an ionised form is a different physical quantity"
        )
    foreign = sorted({a.GetSymbol() for a in mol.GetAtoms()} - _ORGANIC_ELEMENTS)
    if foreign:
        reasons.append(
            f"non-organic element(s) {', '.join(foreign)}; the descriptor contributions are not "
            "defined for them and RDKit sums whatever it recognises rather than refusing"
        )
    return not reasons, tuple(reasons)


def conformal_uncertainty(
    residuals: list[float], *, coverage: float, minimum_samples: int
) -> float | None:
    """The split-conformal half-width covering `coverage` of observed absolute residuals.

    Split conformal in its simplest honest form: with `n` recorded residuals, the interval that
    covers a fraction `coverage` of *future* predictions with finite-sample validity is the
    `ceil((n + 1) · coverage) / n` empirical quantile of the absolute residuals. The `(n + 1)`
    is the whole point — it is what makes the guarantee hold for the next, unseen prediction rather
    than only for the ones already in hand, and dropping it turns a coverage guarantee into an
    in-sample summary that is reliably too narrow.

    Returns None below `minimum_samples`, and when the required quantile falls past the largest
    residual observed. The second case is not an edge case to paper over: with 5 residuals there is
    no finite 95% conformal interval, and returning the maximum residual instead would state a
    guarantee the data cannot support. The caller falls back to the model's reported error and says
    so through `method`.
    """
    if len(residuals) < minimum_samples or not 0 < coverage < 1:
        return None
    ordered = sorted(abs(r) for r in residuals)
    n = len(ordered)
    # Rounded before the ceiling because `(n + 1) * coverage` lands just above an integer for some
    # perfectly ordinary inputs — `75 * 0.68` is `51.00000000000001` — and an unrounded `ceil` then
    # asks for rank 52 rather than 51, reporting an interval one residual wider than the guarantee
    # requires. An overstated uncertainty is the quieter of the two ways to be wrong and the one
    # nobody investigates. The rounding is at the ninth decimal: far below any coverage anyone
    # configures, far above the drift.
    rank = math.ceil(round((n + 1) * coverage, 9))
    if rank > n:
        return None
    return ordered[rank - 1]  # 1-indexed rank → 0-indexed position
