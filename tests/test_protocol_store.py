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
from typing import Any, TypeVar, get_args

import psycopg
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
            await store.set_status(
                _id(backend, "ghost"), "approved", expected_revision=1, actor="chemist-a"
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_set_status_moves_a_design_a_write_never_would(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "status-move")
        await store.append(
            design_id, _design(), [], kind="protocol", author_kind="agent", status="draft"
        )
        await store.set_status(design_id, "approved", expected_revision=1, actor="chemist-a")
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_one_automatic_status_transition_and_the_two_that_are_not(backend: str) -> None:
    """Two transitions happen on a write, and the rest are a human's.

    A structured ask stays `requested`; the first protocol revision makes it a `draft`; and a
    revision landing on an **approved** design takes it back to `draft`, because an approval is a
    statement about a document and the document has changed. That last one is a correction: holding
    the status let a chemist approve revision 1 at 80 °C, an agent draft revision 2 at 200 °C, and
    the header keep reading `approved` over conditions nobody had read — with `GET /protocols/{id}`
    serving the head. `abandoned` is deliberately held, because a design somebody decided not to run
    does not come back because an agent wrote to it.
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

        await store.set_status(design_id, "approved", expected_revision=2)
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
        assert third is not None and third.status == "draft", (
            "a revision landing on an approved design leaves it approved, so the header vouches "
            "for a document nobody signed off"
        )

        await store.set_status(design_id, "abandoned", expected_revision=3)
        await store.append(
            design_id,
            _design(),
            [],
            kind="protocol",
            author_kind="agent",
            parent_revision=3,
            change_note="an agent wrote to it anyway",
            status="draft",
        )
        fourth = await store.summary(design_id)
        assert fourth is not None and fourth.status == "abandoned"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_every_status_the_type_allows_is_a_status_the_schema_accepts(backend: str) -> None:
    """Two `CHECK (status IN (...))` constraints now restate `DesignStatus`, in SQL.

    Neither can be derived from the Literal, so a sixth status added in Python would pass mypy,
    pass every in-memory test, and be rejected by Postgres at runtime — on the write, in front of a
    chemist. Driving all five through the real store is what ties the three declarations together.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "allstatuses")
        design = _design(arms=1)
        await store.append(
            design_id, design, run_checks(design), kind="protocol", author_kind="agent"
        )
        for status in get_args(DesignStatus):
            await store.set_status(
                design_id,
                status,
                expected_revision=1,
                actor="chemist-a",
                reason=f"moving to {status}",
            )
            summary = await store.summary(design_id)
            assert summary is not None and summary.status == status

        recorded = [event.status for event in await store.status_history(design_id)]
        assert recorded == list(reversed(get_args(DesignStatus)))

    _run(_body)


def test_advanced_states_the_rule_the_stores_both_implement() -> None:
    """The one function both backends read, so the transition cannot differ between them."""
    assert advanced("requested", "protocol") == "draft"
    assert advanced("requested", "request") == "requested"
    # An approval names a document, and any revision replaces the document — including a `request`
    # one, since correcting the ask a protocol was approved against un-approves it just as surely.
    assert advanced("approved", "protocol") == "draft"
    assert advanced("approved", "request") == "draft"
    # **`executed` is the same sentence one word along**, and this assertion used to read the other
    # way — written from the same belief as the code it was checking. A header saying a design was
    # run, over a document that was not, is the `approved` defect with a worse word in it.
    assert advanced("executed", "protocol") == "draft"
    assert advanced("executed", "request") == "draft"
    # The two that are held. `abandoned` is the one worth stating: it must not be revived by a
    # write, only by a person.
    for status in ("draft", "abandoned"):
        assert advanced(status, "protocol") == status
        assert advanced(status, "request") == status


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_status_move_records_which_revision_it_was_made_against(backend: str) -> None:
    """The record `advanced()`'s docstring claimed and `set_status` did not keep.

    An approval is retired by the next revision, correctly — so unless the move itself is recorded
    against a revision, "which document did the chemist approve?" has no answer anywhere. Before
    this, `set_status` wrote one column on the header row and logged a line without the revision
    in it.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "signoff")
        design = _design(arms=2)
        await store.append(
            design_id, design, run_checks(design), kind="protocol", author_kind="agent"
        )
        await store.set_status(
            design_id,
            "approved",
            expected_revision=1,
            actor="chemist-a",
            reason="80 C is the precedent",
        )

        # The revision that un-approves it.
        await store.append(
            design_id,
            design,
            run_checks(design),
            kind="protocol",
            author_kind="agent",
            parent_revision=1,
            change_note="an agent redrafted it at 200 C",
        )
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "draft"

        events = await store.status_history(design_id)
        assert len(events) == 1
        assert events[0].status == "approved"
        assert events[0].revision == 1
        assert events[0].actor == "chemist-a"
        assert events[0].reason == "80 C is the precedent"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_status_history_is_newest_first_and_empty_before_any_move(backend: str) -> None:
    """Newest first, because a reader asks what a design's state is now and why."""

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "signoffs")
        design = _design(arms=1)
        await store.append(
            design_id, design, run_checks(design), kind="protocol", author_kind="agent"
        )
        assert await store.status_history(design_id) == []

        await store.set_status(
            design_id, "approved", expected_revision=1, actor="chemist-a", reason="fine"
        )
        await store.set_status(
            design_id, "executed", expected_revision=1, actor="chemist-b", reason="ran it Tuesday"
        )
        events = await store.status_history(design_id)
        assert [event.status for event in events] == ["executed", "approved"]
        assert [event.reason for event in events] == ["ran it Tuesday", "fine"]
        assert {event.revision for event in events} == {1}

    _run(_body)


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


def test_two_writers_racing_on_one_head_lose_as_a_revision_conflict() -> None:
    """The loser of a real race is told the same thing a stale `parent_revision` is told.

    Postgres only, because the race is one `core.db`'s READ COMMITTED connections make possible and
    a single-threaded dict cannot: both writers read `head=1`, both build revision 2, and what
    actually decided between them was the `(design_id, revision)` primary key. That surfaced as a
    raw `psycopg.errors.UniqueViolation` nothing translated, so the second of "two chemists editing
    one plate" got a **500 with no `revision_conflict` code** from `POST
    /protocols/{id}/revisions` — precisely the case the 409 exists for. No artificial barrier is
    needed and none is used: `asyncio.gather` over two real appends reproduces it, which is how it
    was found.
    """

    async def _body() -> None:
        await migrated_db_or_skip()
        store = PostgresDesignStore()
        design_id = _id("postgres", "race")
        await store.append(design_id, _design(), [], kind="protocol", author_kind="agent")

        outcomes = await asyncio.gather(
            store.append(
                design_id,
                _design(arms=2),
                [],
                kind="protocol",
                author_kind="human",
                parent_revision=1,
                change_note="alice, from revision 1",
            ),
            store.append(
                design_id,
                _design(arms=3),
                [],
                kind="protocol",
                author_kind="human",
                parent_revision=1,
                change_note="bob, from the same revision 1",
            ),
            return_exceptions=True,
        )
        refusals = [item for item in outcomes if isinstance(item, BaseException)]
        winners = [item for item in outcomes if not isinstance(item, BaseException)]
        assert len(refusals) == 1 and len(winners) == 1, (
            "both writers built revision 2 from head 1; exactly one of them has to be refused"
        )

        loser = refusals[0]
        assert isinstance(loser, RevisionConflict)
        # The whole defect, stated as its own assertion: a `UniqueViolation` escaping here is what
        # the route turns into a 500, and `RevisionConflict` is not one, so this cannot pass by
        # inheritance.
        assert not isinstance(loser, psycopg.errors.UniqueViolation)
        assert "revision 2" in str(loser)

        # And exactly one revision 2 exists — the winner's, whichever it was.
        history = await store.history(design_id)
        assert [item.revision for item in history] == [1, 2]
        assert history[-1].change_note == winners[0].change_note

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_session_that_created_a_design_is_the_one_the_listing_filters_on(
    backend: str,
) -> None:
    """`session_id` is set once, by the write that opened the design, on **both** backends.

    They disagreed: `InMemoryDesignStore` overwrote it on every append while `_UPSERT_DESIGN` omits
    it from its `DO UPDATE SET` and keeps the creator's — so `listing(session_id=…)` returned
    different designs depending on which backend was configured. The in-memory store is "a real
    backend, not a test double", which is exactly why that is a wrong answer on one of them rather
    than a harmless difference. `opened_by` is asserted beside it because it is the same rule and
    the same omission.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "session-owner")
        await store.append(
            design_id,
            _design(),
            [],
            kind="request",
            author_kind="agent",
            author="chemist-a",
            session_id="one",
            status="requested",
        )
        await store.append(
            design_id,
            _design(arms=2),
            [],
            kind="protocol",
            author_kind="human",
            author="chemist-b",
            parent_revision=1,
            change_note="a second session opened the same design",
            session_id="two",
        )

        summary = await store.summary(design_id)
        assert summary is not None
        assert summary.head_revision == 2
        assert summary.opened_by == "chemist-a"

        # `session_id` is not on the summary row, so the listing filter is where it is observable —
        # and it is also the caller that got the wrong answer.
        assert design_id in {row.design_id for row in await store.listing(session_id="one")}
        assert design_id not in {row.design_id for row in await store.listing(session_id="two")}

    _run(_body)
