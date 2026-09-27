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
from pathlib import Path
from typing import Any, TypeVar

import psycopg
import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "-inf"])
def test_a_non_finite_value_is_refused_before_it_can_land(value: object) -> None:
    """The store is append-only, so what the model accepts is permanent.

    A `NaN` read as a disagreement with itself (`nan != nan`) and reached a surrogate through
    `observations_for` as a measured value; the tool-call strings "NaN" and "inf" parse to exactly
    that.
    """
    with pytest.raises(ValueError, match="finite"):
        ArmResult.model_validate({"arm_id": "A1", "outcome": "yield_pct", "value": value})


def test_a_unit_change_is_reported_as_one_rather_than_as_a_numeric_disagreement() -> None:
    """85 % and 0.85 fraction agree; a re-attach that fixes the unit is not a re-measurement."""
    newest = StoredArmResult(
        arm_id="A1", outcome="yield", value=0.85, unit="fraction", revision=1, result_id=2
    )
    oldest = StoredArmResult(
        arm_id="A1", outcome="yield", value=85.0, unit="%", revision=1, result_id=1
    )
    outcomes = summarise(_design(), "design-x", 1, [newest, oldest])
    assert outcomes.disagreements == ["A1 yield: unit 'fraction' and '%'"]


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


# The migration that puts the model's finiteness rule into the table, read from the configured
# directory so the test drives the file the runner applies rather than a copy of its statement.
_FINITE_MIGRATION = Path(settings.sql_migrations_dir) / "108_experiment_arm_result_value_finite.sql"
_INSERT_RAW = (
    "INSERT INTO experiment_arm_results (design_id, revision, arm_id, outcome, value) "
    "VALUES (%s, 1, 'A1', 'yield_pct', %s::float8)"
)
_FINITE_VALIDATED = (
    "SELECT convalidated FROM pg_constraint WHERE conrelid = 'experiment_arm_results'::regclass "
    "AND conname = 'experiment_arm_results_value_finite'"
)


async def _stored_design(prefix: str) -> str:
    """A design row the results table's foreign key can point at."""
    design_id = f"{prefix}-{datetime.now(UTC).timestamp()}"
    await PostgresDesignStore().append(design_id, _design(), [], author_kind="agent", author="t")
    return design_id


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_the_table_refuses_a_non_finite_value_the_model_would_have(value: str) -> None:
    """The database refuses what `ArmResult` refuses, for a writer that is not `ArmResult`.

    A manual `INSERT`, a restore from another system, or a second writer added later.

    Written as raw SQL on purpose: through the store the model refuses first, and this would pass
    without the constraint existing at all.
    """

    async def body() -> None:
        await migrated_db_or_skip()
        design_id = await _stored_design("design-finite")
        async with db.connection(settings.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(_INSERT_RAW, (design_id, value))

    _run(body)


def test_the_finite_check_replays_and_validates_only_rows_that_satisfy_it() -> None:
    """Both arms of 108, and its replay — inside one transaction that is rolled back.

    A database written by an image from before `ArmResult` refused `NaN` may hold one: there the
    constraint must still land (refusing new writes) and stay `NOT VALID` rather than abort the
    whole migration run. Once the row is gone a replay validates it, and a replay over a validated
    constraint leaves it validated — the property a drop-then-add would lose.
    """

    async def body() -> None:
        await migrated_db_or_skip()
        design_id = await _stored_design("design-finite-arms")
        migration = _FINITE_MIGRATION.read_text(encoding="utf-8")
        warnings: list[str] = []

        def collect(diagnostic: psycopg.errors.Diagnostic) -> None:
            if diagnostic.severity_nonlocalized == "WARNING":
                warnings.append(diagnostic.message_primary or "")

        async with db.connection(settings.postgres_dsn) as conn:
            conn.add_notice_handler(collect)
            try:
                await conn.execute(
                    "ALTER TABLE experiment_arm_results "
                    "DROP CONSTRAINT experiment_arm_results_value_finite"
                )
                await conn.execute(_INSERT_RAW, (design_id, "NaN"))
                await conn.execute(migration)
                assert await (await conn.execute(_FINITE_VALIDATED)).fetchall() == [(False,)]
                # The NOT VALID arm says so, naming the count — it used to be silent.
                assert len(warnings) == 1
                assert "left NOT VALID" in warnings[0]
                assert "1 non-finite row(s)" in warnings[0]
                await conn.execute("SAVEPOINT refused")
                with pytest.raises(psycopg.errors.CheckViolation):
                    await conn.execute(_INSERT_RAW, (design_id, "Infinity"))
                await conn.execute("ROLLBACK TO SAVEPOINT refused")
                await conn.execute(
                    "DELETE FROM experiment_arm_results WHERE design_id = %s", (design_id,)
                )
                await conn.execute(migration)
                assert await (await conn.execute(_FINITE_VALIDATED)).fetchall() == [(True,)]
                await conn.execute(migration)
                assert await (await conn.execute(_FINITE_VALIDATED)).fetchall() == [(True,)]
                assert len(warnings) == 1, "the validating arms must not warn"
            finally:
                conn.remove_notice_handler(collect)
                await conn.rollback()

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


def test_observations_refuse_a_column_mixing_units() -> None:
    """A surrogate fitted to percent beside fraction is fitted to numbers that do not compare.

    Which unit is right is the chemist's call, so the refusal names the arms under each.
    """
    rows = [
        StoredArmResult(arm_id="A1", outcome="yield", value=85.0, unit="%", revision=1),
        StoredArmResult(arm_id="A2", outcome="yield", value=0.9, unit="fraction", revision=1),
    ]
    with pytest.raises(ChemclawError, match=r"more than one unit.*'A1'.*'A2'"):
        observations_for(_design(), "yield", rows)
