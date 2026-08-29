"""The revision history, proven identically against both backends.

`InMemoryDesignStore` is **a real backend, not a test double** — it is what a deployment without
Postgres runs on — so every claim below is parametrized over both. That is the only way the
in-memory half of the suite means anything: if the two disagreed, every other test in this feature
would be proving something the deployment does not do.

The claim the whole table exists for is the append-only one: a write derived from anything but the
head is a `RevisionConflict` the writer sees, rather than a silent overwrite of the revision they
did not know about. Two chemists editing one plate is the ordinary case.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import pytest

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.checks import run_checks
from chemclaw.protocols.models import (
    DesignStatus,
    EvidenceRef,
    ExperimentDesign,
    ExperimentRequest,
    ProtocolArm,
)
from chemclaw.protocols.store import (
    DesignStore,
    InMemoryDesignStore,
    PostgresDesignStore,
    RevisionConflict,
    UnknownDesign,
    advanced,
    default_design_store,
)
from tests.pg import migrated_db_or_skip

_T = TypeVar("_T")

#: Both real backends. Parametrized rather than compared in one test, so a failure names which one.
_BACKENDS = ("memory", "postgres")


async def _backend(name: str) -> DesignStore:
    """A fresh store of the named kind, skipping when no database is reachable."""
    if name == "postgres":
        await migrated_db_or_skip()
        return PostgresDesignStore()
    return InMemoryDesignStore()


def _run(coro: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Drive one async body, the way every other store test in this suite does."""
    return asyncio.run(coro())


def _design(
    *, arms: int = 0, cited: bool = True, project: str = "", mode: str = "single"
) -> ExperimentDesign:
    """A design with the two knobs the header row denormalises: arm count and blocker count."""
    return ExperimentDesign(
        request=ExperimentRequest(
            title="SM-3 Suzuki",
            goal="couple the aryl chloride",
            project=project,
            mode=mode,  # type: ignore[arg-type]
        ),
        arms=[ProtocolArm(arm_id=f"A{index}") for index in range(1, arms + 1)],
        evidence=[
            EvidenceRef(kind="precedent", ref="reaction-1", summary="a run like this gave 72%"),
            EvidenceRef(kind="tool", tool="predict_pka", summary="the base is strong enough"),
        ]
        if cited
        else [],
    )


def _id(backend: str, name: str) -> str:
    """A design id unique to this test and this backend — the Postgres schema outlives one test."""
    return f"design-{backend}-{name}"


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_stored_revision_reads_back_whole(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design = _design(arms=2)
        written = await store.append(
            _id(backend, "readback"),
            design,
            run_checks(design),
            kind="protocol",
            author_kind="agent",
            author="chemist-a",
            change_note="drafted the protocol",
        )
        assert written.revision == 1 and written.parent_revision == 0

        read = await store.read(_id(backend, "readback"))
        assert read is not None
        assert read.revision == 1
        assert read.kind == "protocol"
        assert read.author_kind == "agent"
        assert read.author == "chemist-a"
        assert read.change_note == "drafted the protocol"
        assert read.design == design
        assert [check.check_id for check in read.checks] == [
            check.check_id for check in run_checks(design)
        ]

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_reading_an_unknown_design_answers_none(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        assert await store.read(_id(backend, "nothing")) is None
        assert await store.summary(_id(backend, "nothing")) is None
        assert await store.history(_id(backend, "nothing")) == []

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_read_selects_a_specific_revision_and_defaults_to_the_head(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "revisions")
        for revision, note in enumerate(("first", "second", "third"), start=1):
            await store.append(
                design_id,
                _design(arms=revision),
                [],
                kind="protocol",
                author_kind="agent",
                parent_revision=revision - 1,
                change_note=note,
            )

        head = await store.read(design_id)
        assert head is not None and head.revision == 3 and head.change_note == "third"

        first = await store.read(design_id, 1)
        assert first is not None and first.change_note == "first"
        assert len(first.design.arms) == 1

        assert await store.read(design_id, 9) is None

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_history_comes_back_oldest_first(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "history")
        for revision, note in enumerate(("a", "b", "c"), start=1):
            await store.append(
                design_id,
                _design(),
                [],
                kind="protocol",
                author_kind="human" if revision == 2 else "agent",
                parent_revision=revision - 1,
                change_note=note,
            )

        history = await store.history(design_id)
        assert [item.revision for item in history] == [1, 2, 3]
        assert [item.change_note for item in history] == ["a", "b", "c"]
        assert [item.author_kind for item in history] == ["agent", "human", "agent"]
        assert [item.parent_revision for item in history] == [0, 1, 2]

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_write_derived_from_a_stale_revision_is_refused(backend: str) -> None:
    """The whole point of the table: the loser is told, rather than the winner overwritten."""

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "stale")
        await store.append(design_id, _design(), [], kind="protocol", author_kind="agent")
        await store.append(
            design_id, _design(), [], kind="protocol", author_kind="human", parent_revision=1
        )

        with pytest.raises(RevisionConflict, match="is at revision 2"):
            await store.append(
                design_id, _design(), [], kind="protocol", author_kind="human", parent_revision=1
            )

        # And nothing was written by the refusal.
        assert [item.revision for item in await store.history(design_id)] == [1, 2]

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_parent_revision_zero_on_an_existing_design_is_refused(backend: str) -> None:
    """`0` means "I am creating this"; it is not a shortcut for "the head, whatever it is"."""

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "zero-parent")
        await store.append(design_id, _design(), [], kind="protocol", author_kind="agent")

        with pytest.raises(RevisionConflict, match="derived from 0"):
            await store.append(
                design_id, _design(), [], kind="protocol", author_kind="agent", parent_revision=0
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_revision_conflict_is_a_chemclaw_error(backend: str) -> None:
    """It reaches the tool caller and the 409 through the same family every refusal here uses."""
    assert issubclass(RevisionConflict, ChemclawError)
    assert issubclass(UnknownDesign, ChemclawError)
    assert backend in _BACKENDS


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_summary_reflects_the_head_revision_and_its_counts(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "summary")
        blocked = _design(arms=2, cited=False, project="prj-a")
        await store.append(
            design_id,
            blocked,
            run_checks(blocked),
            kind="protocol",
            author_kind="agent",
            author="chemist-a",
        )
        first = await store.summary(design_id)
        assert first is not None
        assert first.design_id == design_id
        assert first.title == "SM-3 Suzuki"
        assert first.project == "prj-a"
        assert first.opened_by == "chemist-a"
        assert first.head_revision == 1
        assert first.arms == 2
        # `evidence_present` is the one blocker an otherwise-empty design fails.
        assert first.blockers == 1

        cured = _design(arms=5, cited=True, project="prj-a")
        await store.append(
            design_id,
            cured,
            run_checks(cured),
            kind="protocol",
            author_kind="human",
            parent_revision=1,
            change_note="cited the precedent",
        )
        second = await store.summary(design_id)
        assert second is not None
        assert (second.head_revision, second.arms, second.blockers) == (2, 5, 0)

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_listing_filters_by_status_and_project_and_is_newest_first(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        rows: list[tuple[str, str, DesignStatus]] = [
            (_id(backend, "list-a"), "prj-a", "requested"),
            (_id(backend, "list-b"), "prj-b", "draft"),
            (_id(backend, "list-c"), "prj-a", "draft"),
        ]
        for design_id, project, status in rows:
            await store.append(
                design_id,
                _design(project=project),
                [],
                kind="request" if status == "requested" else "protocol",
                author_kind="agent",
                status=status,
            )
            # The order the listing reports is `updated_at DESC`, and two appends inside one
            # millisecond would make that ordering a coin toss rather than a claim.
            await asyncio.sleep(0.01)

        mine = {row[0] for row in rows}
        listed = [s.design_id for s in await store.listing() if s.design_id in mine]
        assert listed == [rows[2][0], rows[1][0], rows[0][0]]

        drafts = {s.design_id for s in await store.listing(status="draft")} & mine
        assert drafts == {rows[1][0], rows[2][0]}

        project_a = {s.design_id for s in await store.listing(project="prj-a")} & mine
        assert project_a == {rows[0][0], rows[2][0]}

        both = [
            s.design_id
            for s in await store.listing(status="draft", project="prj-a")
            if s.design_id in mine
        ]
        assert both == [rows[2][0]]

        assert len(await store.listing(limit=1)) == 1

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_set_status_on_an_unknown_design_is_refused(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        with pytest.raises(UnknownDesign, match="no design"):
            await store.set_status(_id(backend, "ghost"), "approved", "chemist-a")

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_set_status_moves_a_design_a_write_never_would(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "status-move")
        await store.append(
            design_id, _design(), [], kind="protocol", author_kind="agent", status="draft"
        )
        await store.set_status(design_id, "approved", "chemist-a")
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_one_automatic_status_transition_and_the_two_that_are_not(backend: str) -> None:
    """One transition happens on a write, and the rest are a human's.

    A structured ask stays `requested`; the first protocol revision makes it a `draft`; a later
    protocol revision must not silently un-approve an approved design.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "lifecycle")

        await store.append(
            design_id, _design(), [], kind="request", author_kind="agent", status="requested"
        )
        first = await store.summary(design_id)
        assert first is not None and first.status == "requested"

        await store.append(
            design_id,
            _design(),
            [],
            kind="protocol",
            author_kind="agent",
            parent_revision=1,
            status="draft",
        )
        second = await store.summary(design_id)
        assert second is not None and second.status == "draft"

        await store.set_status(design_id, "approved")
        await store.append(
            design_id,
            _design(),
            [],
            kind="protocol",
            author_kind="human",
            parent_revision=2,
            change_note="the chemist raised the temperature",
            status="draft",
        )
        third = await store.summary(design_id)
        assert third is not None and third.status == "approved"

    _run(_body)


def test_advanced_states_the_rule_the_stores_both_implement() -> None:
    """The one function both backends read, so the transition cannot differ between them."""
    assert advanced("requested", "protocol") == "draft"
    assert advanced("requested", "request") == "requested"
    for status in ("draft", "approved", "executed", "abandoned"):
        assert advanced(status, "protocol") == status
        assert advanced(status, "request") == status


def test_the_default_store_follows_the_session_store_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same switch the audit sink, the job record and the campaign store read."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_design_store(), PostgresDesignStore)

    monkeypatch.setattr(settings, "session_store", "memory")
    memory = default_design_store()
    assert isinstance(memory, InMemoryDesignStore)
    # Module-level rather than per-call: a real backend that forgot every design between two calls
    # would be worse than none at all.
    assert default_design_store() is memory


def test_both_backends_satisfy_the_declared_protocol() -> None:
    """`DesignStore` is `runtime_checkable`, so the two implementations answer to one name."""
    assert isinstance(InMemoryDesignStore(), DesignStore)
    assert isinstance(PostgresDesignStore(), DesignStore)
