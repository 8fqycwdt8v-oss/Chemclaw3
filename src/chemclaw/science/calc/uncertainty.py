"""One shape for "how much should this number be trusted", across every calculator.

Three different questions:

- *How wrong is this likely to be?* — `uncertainty`, with `method` saying how it was obtained:
  `reported` (the model's published error, independent of this molecule) or `propagated` (an input's
  uncertainty carried through the arithmetic, scaled by the derivative — e.g. logD scales the pKa
  residual by the ionised fraction).
- *Can the model speak about this molecule at all?* — `in_domain`. An out-of-domain error bar is
  meaningless, not merely larger.
- *Do we know?* — `None`, distinct from `False`: no declared domain is "unknown".

Only a structural domain (what a model's terms are defined over) can be asserted without the
training set; this repository ships none, so no statistical domain is invented. The structural
screen runs with the prediction in `Chemclaw3-mcp`'s `calc` server, and `SolubilityResult.estimate`
arrives carrying its verdict.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.errors import ChemclawError

# How an uncertainty was arrived at; a reviewer weighs a published constant differently from a
# propagated one.
Method = Literal["reported", "propagated", "none"]

# How an uncertainty was obtained, in the words a note reader sees; beside `Method` so every
# method has prose.
_METHOD_PROSE: dict[Method, str] = {
    "reported": "the model's own reported error",
    "propagated": "propagated from the inputs",
    "none": "no uncertainty established",
}


class CalculationDomainError(ChemclawError):
    """A calculator refuses a molecule it cannot speak about, and says why.

    The refusal end of `Estimate.in_domain`: there is no number to give (e.g. no basic pKa without a
    protonatable nitrogen). A `ChemclawError` so `agent.tool_authz.surface_domain_errors` shows the
    message to the model verbatim; it must be caller-safe, explaining the limit in the chemist's
    terms.
    """


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
    # None when nothing about this prediction's error is known; 0.0 would claim it is exact.
    uncertainty: float | None = None
    method: Method = "none"
    # None = this calculator declares no applicability domain: unanswered, not "yes".
    in_domain: bool | None = None
    # Why not, in a chemist's terms, one reason per failed check. Empty when in domain or unknown.
    domain_reasons: tuple[str, ...] = ()

    @property
    def trustworthy(self) -> bool:
        """Whether a consumer may use this number without a human looking at it first.

        Requires an affirmative domain answer: `None` means nobody checked.
        """
        return self.in_domain is True

    def render(self, *, fmt: str = ".6g") -> str:
        """This number and how far to trust it, as one inline fragment of a note body.

        Inline because retrieval excerpts truncate note bodies, and a footer would be cut. An
        in-domain estimate adds no remark; out-of-domain and "not assessed" are both spelled out.

        Args:
            fmt: Format spec for the value and its uncertainty; the caller owns precision.
        """
        number = f"{self.value:{fmt}}"
        if self.uncertainty is not None:
            number += f" ± {self.uncertainty:{fmt}}"
        text = f"{number} {self.unit} ({_METHOD_PROSE[self.method]})"
        if self.in_domain is None:
            return f"{text}; applicability not assessed"
        if not self.in_domain:
            return f"{text}; OUT OF DOMAIN — {'; '.join(self.domain_reasons)}"
        return text
