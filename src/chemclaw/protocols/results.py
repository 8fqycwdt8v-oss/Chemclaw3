"""What a designed arm produced: the join between the prescriptive tier and the numbers.

`protocols/` says what to run and `ingest/eln` records what was done, and until now nothing
connected them. A plate was laid out here, exported as a run sheet, run, and its numbers came back
into somebody's spreadsheet — so the round trip `skills/hte-campaign-design` promises in its own
closing section, *"the arms that survive become the observations `suggest_next_experiment` fits a
surrogate to"*, was a person retyping a table.

**The key is (design, revision, arm), and that choice is what makes an externally-run plate
attachable.** A chemist who ran the run sheet in another lab has a `design_id` and an `arm_id` and
nothing else; this module asks for nothing else, and `reaction_id` is optional so an ELN
transcription links when one exists and is absent when the run lives on paper.

**The revision is part of the key rather than the head.** A plate is run from the revision a
chemist printed. A later edit that drops a factor level must not silently re-point last week's
numbers at arms that no longer mean the same thing, so an outcome names the revision it was
measured against and a reader can see when the two have diverged.

**Append-only**, like the revisions beside it and for a related reason: a re-measured well is a
second observation, not a correction. An assay repeated on a degraded sample is data about the
sample, and overwriting would delete the evidence that the two disagree. `latest_by_arm` takes the
newest per (arm, outcome) and `disagreements` is what makes the rest visible rather than lost.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.models import AuthorKind, ExperimentDesign


class UnknownArm(ChemclawError):
    """An outcome names an arm the stored revision does not have.

    Checked here rather than in SQL because the arms live inside a JSONB document: a foreign key
    that cannot see them would be a control whose condition never occurs, and the refusal a caller
    needs is one that names the arms that *do* exist.
    """


class ArmResult(BaseModel):
    """One measured outcome for one arm of one revision.

    `outcome` is deliberately free text in the design's own vocabulary rather than an enum: a plate
    measures whatever its `analytics.measures` said it would, and an enum here would be a second
    vocabulary for the same facts — which is how an objective the plate was run for stops matching
    the number that answers it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    # Finite, because the store is append-only: a `NaN` accepted here (pydantic takes the strings
    # "NaN" and "inf" too) lands permanently, reads as a disagreement with itself (`nan != nan`),
    # and reaches a surrogate through `observations_for` as a measured value.
    value: float = Field(allow_inf_nan=False)
    # As `core/units` spells it. Carried rather than assumed: a yield in percent and an assay in
    # mg/mL are both numbers, and only one of them is comparable to a specification limit.
    unit: str = ""
    # The ELN transcription of this run, when there is one. Absent is the ordinary case.
    reaction_id: str = ""
    measured_at: datetime | None = None
    note: str = ""


class StoredArmResult(ArmResult):
    """An `ArmResult` as the store holds it, with who attached it and when."""

    result_id: int = 0
    revision: int = Field(default=0, ge=0)
    author_kind: AuthorKind = "human"
    author: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlateOutcomes(BaseModel):
    """Every outcome attached to one design, and what the set of them adds up to.

    The counts are computed rather than left to a caller, because "eleven of twenty-four arms have
    a yield" is the sentence that decides whether a campaign can be fitted at all, and a caller
    that has to derive it from a list will sometimes not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_id: str = ""
    revision: int = 0
    results: list[StoredArmResult] = Field(default_factory=list)
    # Arms of that revision with no outcome at all. Named rather than counted: which well is
    # missing is what a chemist needs, and a count is what makes a half-run plate look finished.
    arms_without_results: list[str] = Field(default_factory=list)
    # (arm, outcome) pairs measured more than once with different values — kept visible because the
    # append-only shape exists to keep them.
    disagreements: list[str] = Field(default_factory=list)


def require_arms_exist(design: ExperimentDesign, results: Iterable[ArmResult]) -> None:
    """Refuse an outcome naming an arm this revision does not have.

    The one check worth making before a write, because the failure it prevents is silent: an
    outcome attached to a mistyped arm id is stored, counts toward nothing, and leaves the arm it
    was meant for looking unrun. Worse on a plate, where `A1` and `A11` are both plausible.

    Raises:
        UnknownArm: Naming the offending ids and the arms that exist.
    """
    known = {arm.arm_id for arm in design.arms}
    unknown = sorted({result.arm_id for result in results} - known)
    if unknown:
        raise UnknownArm(
            f"these arm id(s) are not in this revision of the design: {unknown}. "
            f"Its arms are: {sorted(known)}"
        )


def latest_by_arm(results: Sequence[StoredArmResult]) -> dict[tuple[str, str], StoredArmResult]:
    """The newest result per (arm, outcome), which is what a reader almost always wants.

    `results` is expected newest-first, as the store returns it, so the first of each key wins.
    """
    latest: dict[tuple[str, str], StoredArmResult] = {}
    for result in results:
        latest.setdefault((result.arm_id, result.outcome), result)
    return latest


def summarise(
    design: ExperimentDesign, design_id: str, revision: int, results: Sequence[StoredArmResult]
) -> PlateOutcomes:
    """Fold a design's stored outcomes into the three facts a reader needs.

    The unmeasured arms are named rather than counted for `analytical.evaluate`'s reason about
    `not_measured`: an arm nothing measured is an unanswered question, and a summary that reports
    only what it has makes a half-run plate look finished.
    """
    latest = latest_by_arm(results)
    measured = {arm for arm, _ in latest}
    seen: dict[tuple[str, str], StoredArmResult] = {}
    disagreements: list[str] = []
    for result in results:
        key = (result.arm_id, result.outcome)
        first = seen.setdefault(key, result)
        # A unit change is its own line rather than a numeric disagreement: 85 % and 0.85 fraction
        # agree, and reporting them as "85 and 0.85" would send a chemist to re-run a well that
        # was only relabelled.
        if first.unit != result.unit:
            disagreements.append(
                f"{result.arm_id} {result.outcome}: unit {first.unit!r} and {result.unit!r}"
            )
        elif first.value != result.value:
            disagreements.append(
                f"{result.arm_id} {result.outcome}: {first.value} and {result.value}"
            )
    return PlateOutcomes(
        design_id=design_id,
        revision=revision,
        results=list(results),
        arms_without_results=sorted(
            arm.arm_id for arm in design.arms if arm.arm_id not in measured
        ),
        disagreements=sorted(set(disagreements)),
    )


def observations_for(
    design: ExperimentDesign, outcome: str, results: Sequence[StoredArmResult]
) -> list[dict[str, float | str]]:
    """Each measured arm's factor levels plus its outcome, ready to seed a campaign.

    **This is the loop's payoff and the reason the join exists.** `experiment_arms_from_campaign`
    already turns a campaign's suggested points into a design; this is the other direction, and
    without it a chemist who ran a plate this system laid out had to retype the table before
    `suggest_next_experiment` could fit anything to it.

    Returns one row per arm that has a value for `outcome`, with the arm's declared factor levels
    beside it. Arms with no measurement are omitted rather than defaulted — a missing well is not a
    zero, and a surrogate fitted to invented zeros is worse than one fitted to fewer points.

    Raises:
        ChemclawError: the latest values for `outcome` carry more than one unit, naming the arms
            under each. A column mixing percent and fraction fits a surrogate to numbers that are
            not comparable, and which unit is right is the chemist's call, not a conversion here.
    """
    latest = latest_by_arm(results)
    measured = [
        (arm, found)
        for arm in design.arms
        if (found := latest.get((arm.arm_id, outcome))) is not None
    ]
    by_unit: dict[str, list[str]] = {}
    for arm, found in measured:
        by_unit.setdefault(found.unit, []).append(arm.arm_id)
    if len(by_unit) > 1:
        raise ChemclawError(
            f"the latest {outcome!r} values are in more than one unit: "
            + "; ".join(f"{unit or '(no unit)'}: {sorted(arms)}" for unit, arms in by_unit.items())
            + ". Re-attach them in one unit before seeding a campaign"
        )
    rows: list[dict[str, float | str]] = []
    for arm, found in measured:
        row: dict[str, float | str] = dict(arm.levels)
        row[outcome] = found.value
        rows.append(row)
    return rows
