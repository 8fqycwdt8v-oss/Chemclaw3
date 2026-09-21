"""The plate-results loop: attaching outcomes to designed arms, and reading them back as a campaign.

Both real backends, parametrized rather than compared in one test so a failure names which one —
`tests/test_protocol_store.py`'s convention, and the in-memory one is a real backend for a
deployment without Postgres rather than a double.

Most of this file is about the three things the numbers alone do not say: which arms have **no**
result, which wells were measured twice and disagree, and that an unmeasured arm is omitted from a
campaign's observations rather than defaulted to zero.
"""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

import pytest

from chemclaw.protocols.models import (
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    Factor,
    FactorLevel,
    ProtocolArm,
)
from chemclaw.protocols.result_store import (
    InMemoryArmResultStore,
    PostgresArmResultStore,
    default_arm_result_store,
)
from chemclaw.protocols.results import (
    ArmResult,
    StoredArmResult,
    UnknownArm,
    latest_by_arm,
    observations_for,
    require_arms_exist,
    summarise,
)
from chemclaw.protocols.store import PostgresDesignStore
from tests.pg import migrated_db_or_skip

_T = TypeVar("_T")

_BACKENDS = ("memory", "postgres")


def _run(coro: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Drive one async body, the way every other store test in this suite does."""
    return asyncio.run(coro())


def _design(arms: int = 3) -> ExperimentDesign:
    """A one-factor screen with `arms` arms, each at its own ligand level."""
    ligands = ["XPhos", "SPhos", "RuPhos", "BrettPhos"][:arms]
    return ExperimentDesign(
        request=ExperimentRequest(title="SM-3 screen", goal="find a ligand", mode="screen"),
        factors=[
            Factor(
                name="ligand",
                kind="categorical",
                levels=[FactorLevel(label=name) for name in ligands],
            )
        ],
        arms=[
            ProtocolArm(arm_id=f"A{index + 1}", levels={"ligand": name})
            for index, name in enumerate(ligands)
        ],
        evidence=[
            EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
            EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
        ],
    )


def _result(arm: str, value: float, outcome: str = "yield_pct") -> ArmResult:
    return ArmResult(arm_id=arm, outcome=outcome, value=value, unit="%")


# --- the arm check ------------------------------------------------------------------------------


def test_an_outcome_naming_an_arm_the_revision_lacks_is_refused() -> None:
    """The failure this prevents is silent, which is why it is worth a check at all.

    An outcome attached to a mistyped arm id stores, counts toward nothing, and leaves the arm it
    was meant for looking unrun. On a plate `A1` and `A11` are both plausible.
    """
    with pytest.raises(UnknownArm, match="A11"):
        require_arms_exist(_design(), [_result("A11", 61.0)])


def test_the_refusal_names_the_arms_that_do_exist() -> None:
    """A refusal a caller cannot act on is one they retry verbatim."""
    with pytest.raises(UnknownArm) as caught:
        require_arms_exist(_design(), [_result("A9", 1.0)])
    assert "'A1', 'A2', 'A3'" in str(caught.value)


# --- the summary --------------------------------------------------------------------------------


def test_unmeasured_arms_are_named_rather_than_counted() -> None:
    """A summary of only what landed makes a half-run plate look finished.

    `analytical.evaluate`'s argument about `not_measured`, one tier over: an arm nothing measured is
    an unanswered question, and which one it is is what a chemist needs.
    """
    stored = [StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1)]
    outcomes = summarise(_design(), "design-x", 1, stored)
    assert outcomes.arms_without_results == ["A2", "A3"]


def test_two_measurements_of_one_well_that_disagree_stay_visible() -> None:
    """The reason the table is append-only rather than upserted.

    A re-measured well is a second observation, not a correction: an assay repeated on a degraded
    sample is data about the sample, and overwriting would delete the evidence that the two
    disagree.
    """
    newest = StoredArmResult(**_result("A1", 58.0).model_dump(), revision=1, result_id=2)
    oldest = StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1)
    outcomes = summarise(_design(), "design-x", 1, [newest, oldest])
    assert outcomes.disagreements == ["A1 yield_pct: 58.0 and 61.0"]


def test_two_measurements_that_agree_are_not_a_disagreement() -> None:
    """Otherwise a re-run confirming a number reads as a contradiction."""
    rows = [
        StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=2),
        StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1),
    ]
    assert summarise(_design(), "design-x", 1, rows).disagreements == []


def test_the_latest_measurement_per_arm_and_outcome_wins() -> None:
    """Newest-first is the store's contract, and this is what depends on it."""
    rows = [
        StoredArmResult(**_result("A1", 58.0).model_dump(), revision=1, result_id=2),
        StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1),
    ]
    assert latest_by_arm(rows)[("A1", "yield_pct")].value == 58.0


def test_two_outcomes_on_one_arm_are_separate_keys() -> None:
    """A plate measuring yield and purity has two numbers per well, not one that overwrites."""
    rows = [
        StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=2),
        StoredArmResult(**_result("A1", 99.1, "purity_pct").model_dump(), revision=1, result_id=1),
    ]
    latest = latest_by_arm(rows)
    assert latest[("A1", "yield_pct")].value == 61.0
    assert latest[("A1", "purity_pct")].value == 99.1


# --- the campaign handoff -----------------------------------------------------------------------


def test_observations_carry_each_measured_arms_factor_levels() -> None:
    """The loop's payoff: the shape `suggest_next_experiment` fits a surrogate to.

    Without it a chemist who ran a plate this system laid out had to retype the table before
    anything could be fitted to it.
    """
    rows = [
        StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1),
        StoredArmResult(**_result("A3", 70.0).model_dump(), revision=1, result_id=2),
    ]
    assert observations_for(_design(), "yield_pct", rows) == [
        {"ligand": "XPhos", "yield_pct": 61.0},
        {"ligand": "RuPhos", "yield_pct": 70.0},
    ]


def test_an_unmeasured_arm_is_omitted_from_observations_rather_than_zeroed() -> None:
    """A missing well is not a zero.

    The most damaging thing this module could do is hand a surrogate a fabricated zero for a well
    nobody ran: the fit would then be confidently wrong in the direction of that arm's conditions,
    and nothing downstream could see why.
    """
    rows = [StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1)]
    observations = observations_for(_design(), "yield_pct", rows)
    assert observations == [{"ligand": "XPhos", "yield_pct": 61.0}]


def test_observations_for_an_outcome_nothing_measured_are_empty_not_invented() -> None:
    rows = [StoredArmResult(**_result("A1", 61.0).model_dump(), revision=1, result_id=1)]
    assert observations_for(_design(), "purity_pct", rows) == []


# --- the store ----------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS)
def test_results_round_trip_through_both_backends(backend: str) -> None:
    """Appended newest-last, read newest-first, which is what `latest_by_arm` rests on."""

    async def body() -> None:
        if backend == "postgres":
            await migrated_db_or_skip()
            design_store = PostgresDesignStore()
            store = PostgresArmResultStore()
            design_id = f"design-results-{datetime.now(UTC).timestamp()}"
            await design_store.append(
                design_id, _design(), [], author_kind="agent", author="tester"
            )
        else:
            store = InMemoryArmResultStore()  # type: ignore[assignment]
            design_id = "design-memory"
        attached = await store.append(
            design_id,
            1,
            [_result("A1", 61.0), _result("A2", 12.0)],
            author_kind="agent",
            author="tester",
        )
        assert attached == 2
        rows = await store.read(design_id)
        assert {row.arm_id for row in rows} == {"A1", "A2"}
        assert {row.unit for row in rows} == {"%"}

    _run(body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_revision_narrows_the_read(backend: str) -> None:
    """A plate is run from the revision a chemist printed, so outcomes are read per revision.

    A later edit that drops a factor level must not silently re-point last week's numbers at arms
    that no longer mean the same thing.
    """

    async def body() -> None:
        if backend == "postgres":
            await migrated_db_or_skip()
            design_store = PostgresDesignStore()
            store = PostgresArmResultStore()
            design_id = f"design-rev-{datetime.now(UTC).timestamp()}"
            await design_store.append(
                design_id, _design(), [], author_kind="agent", author="tester"
            )
        else:
            store = InMemoryArmResultStore()  # type: ignore[assignment]
            design_id = "design-memory-rev"
        await store.append(design_id, 1, [_result("A1", 61.0)], author_kind="agent")
        await store.append(design_id, 2, [_result("A2", 12.0)], author_kind="agent")
        assert [row.arm_id for row in await store.read(design_id, 1)] == ["A1"]
        assert [row.arm_id for row in await store.read(design_id, 2)] == ["A2"]
        assert len(await store.read(design_id)) == 2

    _run(body)


def test_an_empty_append_writes_nothing_and_says_so() -> None:
    """Attaching an empty plate is a caller mistake, not a database round trip."""

    async def body() -> None:
        store = InMemoryArmResultStore()
        assert await store.append("design-x", 1, [], author_kind="agent") == 0
        assert await store.read("design-x") == []

    _run(body)


def test_the_default_store_follows_the_session_store_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same switch `default_design_store` reads, so the two cannot disagree about durability."""
    monkeypatch.setattr("chemclaw.core.config.settings.session_store", "postgres")
    assert isinstance(default_arm_result_store(), PostgresArmResultStore)
    monkeypatch.setattr("chemclaw.core.config.settings.session_store", "memory")
    assert isinstance(default_arm_result_store(), InMemoryArmResultStore)
