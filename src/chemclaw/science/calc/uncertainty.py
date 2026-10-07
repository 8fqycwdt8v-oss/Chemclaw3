"""One shape for "how much should this number be trusted", across every calculator.

`uncertainty` with `method` (`reported` published error, or `propagated` through the arithmetic),
and `in_domain`, where `None` means no declared domain ("unknown", not "yes"). Only structural
domains are asserted, and the screen runs with the prediction on the server.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.errors import ChemclawError

# How an uncertainty was arrived at; a reviewer weighs a published constant differently from a
# propagated one.
Method = Literal["reported", "propagated", "none"]

# How an uncertainty was obtained, in the words a note reader sees; beside `Method` so every method
# has prose.
_METHOD_PROSE: dict[Method, str] = {
    "reported": "the model's own reported error",
    "propagated": "propagated from the inputs",
    "none": "no uncertainty established",
}


class CalculationDomainError(ChemclawError):
    """A calculator refuses a molecule it cannot speak about, and says why.

    A `ChemclawError`, so the message reaches the model verbatim and must be caller-safe.
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

        Inline because excerpts truncate note bodies. In-domain adds nothing; out-of-domain and "not
        assessed" are spelled out. `fmt` is the value's format spec.
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
