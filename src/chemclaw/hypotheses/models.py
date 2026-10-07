"""The objects a hypothesis tournament moves between its stages.

A hypothesis is a claim with a stated way to be wrong: `refuted_if` is required and non-empty, so
the generator cannot return an unfalsifiable one. That field is also the hypothesis's identity for
`screen.py`'s de-duplication — two statements refuted by the same observation are one hypothesis,
however differently phrased.

An `Objection` carries a rationale or it does not count; it is evidence entered into the
tournament, never a verdict that removes anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How a discriminating check gets answered: by this system's own tools, or by somebody in a lab.
CheckKind = Literal["computable", "physical"]

#: What running a check did to the hypothesis it was chosen to discriminate.
CheckVerdict = Literal["supported", "refuted", "inconclusive", "not-run"]


class Hypothesis(BaseModel):
    """One candidate explanation, with the observation that would refute it.

    `angle` records which generator brief produced it — not to weight it (every angle's output
    enters the tournament on equal terms) but so a chemist reading the table can see that the
    field was not all generated from one framing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    mechanism: str = Field(default="")
    refuted_if: str = Field(min_length=1)
    cited_note_ids: list[str] = Field(default_factory=list)
    angle: str = Field(default="")


class Objection(BaseModel):
    """A stated concern about one hypothesis, with the reasoning that supports it.

    `rationale` is `min_length=1` for the reason in the module docstring: an objection nobody can
    evaluate is not evidence, and admitting one would hand the critic a veto it is explicitly not
    being given.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    concern: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    cited_note_ids: list[str] = Field(default_factory=list)


class CheckCall(BaseModel):
    """A `computable` check as a *call* rather than a sentence: which target, on which subject.

    **Two names and nothing else, which is the point.** The model selects a target — a tool, a
    job or a template — and points at a note; it writes neither the structure nor any other
    argument. The structure is read off the
    resolved note and every remaining argument stays at the tool's own default
    (`hypotheses/dispatch.py` says why at length). A richer call type would be a richer surface for
    inventing values on.

    `subject_note_id` is a pointer to be checked, never trusted: it has to resolve in this
    deployment's corpus to a `compound` note whose structure parses, and it refuses on each miss
    with its own reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # An endpoint-tool call: one tool, one compound. The cheap, cached half.
    tool: str = Field(default="")
    subject_note_id: str = Field(default="")

    # A durable-job call. `subjects` maps a params field of the job's model (`reactants`,
    # `products`, `smiles`) to note ids; each id is resolved against the corpus, so the model
    # writes no structure.
    job: str = Field(default="")
    subjects: dict[str, list[str]] = Field(default_factory=dict)
    # The axis this check varies and its values (a solvent screen is `sweep_parameter="solvents"`).
    # Flat scalars because they cross the Temporal wire inside a structured-output schema. The
    # values are checked against the job's `precondition` before anything runs and reported beside
    # each result.
    sweep_parameter: str = Field(default="")
    sweep_values: list[str] = Field(default_factory=list)

    # A template call: a reviewed procedure chaining an enumerator into a calculation — the only
    # shape that can ask about structures nobody wrote down (tautomers, protonation states). The
    # subject is `subject_note_id`; other inputs stay unset so the template's defaults apply.
    template: str = Field(default="")

    @property
    def named_targets(self) -> list[str]:
        """The targets this call names — one for a well-formed call, none for a `physical` check.

        A property rather than a raising validator: the structured-output schema cannot express
        mutual exclusion of `tool`/`job`/`template`, and raising would drop the hypothesis's check
        silently. The dispatcher refuses an ambiguous call with a code instead.
        """
        return [name for name in (self.tool, self.job, self.template) if name]


class DiscriminatingCheck(BaseModel):
    """The cheapest observation that would separate this hypothesis from its rivals.

    `kind` decides what happens next and nothing else does: `computable` means this system holds a
    tool that answers it, and the tournament tries to run it; `physical` means somebody has to do
    it, and it becomes an `experiment-proposal` note for a human to accept or decline.

    A `computable` check carries a `call` or it is not dispatched. That is deliberate rather than
    defensive: a check the model can describe but not ground is exactly the case where running it
    would mean inventing the arguments, and the refusal — with its reason — is what the chemist
    sees instead of a number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    kind: CheckKind
    tool: str = Field(default="")
    call: CheckCall | None = None
    expectation: str = Field(min_length=1)


class CheckOutcome(BaseModel):
    """What running a `computable` check actually produced.

    `verdict` is `inconclusive` far more often than it is `refuted`, and that is the honest result
    rather than a degraded one: a semiempirical number inside its own error bar settles nothing, and
    `CLAUDE.md` is explicit that saying so is the required behaviour where there is no tier to
    escalate to.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    verdict: CheckVerdict = "not-run"
    detail: str = Field(default="")
    calc_refs: list[str] = Field(default_factory=list)
    # The refusal vocabulary from `hypotheses/dispatch.Refusal`, empty when the check ran; separate
    # from `detail` so a deployment can count why checks are not running.
    refusal_code: str = Field(default="")
    # What the tool was asked and what was left at its default, so a reader can tell which
    # conditions (e.g. solvent) the number was computed under.
    ran: str = Field(default="")


class ScreenRejection(BaseModel):
    """A hypothesis the mechanical screen removed, and which rule removed it.

    Rejections are reported, never silent. A generator producing eight unfalsifiable hypotheses is
    a fact about the run that a reader needs, and a screen that quietly dropped them would look
    identical to a generator that produced eight good ones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    detail: str = Field(default="")


class ScreenMerge(BaseModel):
    """Two hypotheses the screen judged to be one, and the one that survived."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kept: str = Field(min_length=1)
    merged: str = Field(min_length=1)
    similarity: float = Field(ge=0.0, le=1.0)


class RankedHypothesis(BaseModel):
    """One row of the answer: a hypothesis, where it placed, and everything held against it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis: Hypothesis
    rating: float
    standard_error: float
    comparisons: int
    objections: list[Objection] = Field(default_factory=list)
    check: DiscriminatingCheck | None = None
    outcome: CheckOutcome | None = None


class TournamentOutcome(BaseModel):
    """The whole result: the ranked field, what was thrown away, and what is not yet settled.

    `leader_is_decisive` is deliberately a field rather than something a reader infers from the
    ratings. The top two being inside their own uncertainty is the common case in a small
    tournament, and a table that renders a strict order without saying so invites a chemist to act
    on a gap that is an artefact of which comparisons happened to run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    ranked: list[RankedHypothesis] = Field(default_factory=list)
    rejected: list[ScreenRejection] = Field(default_factory=list)
    merged: list[ScreenMerge] = Field(default_factory=list)
    comparisons_run: int = Field(default=0, ge=0)
    position_bias: float | None = None
    leader_is_decisive: bool = False
    proposal_note_ids: list[str] = Field(default_factory=list)
