"""The revision history, proven identically against both backends.

`InMemoryDesignStore` is a real backend a deployment without Postgres runs on, so every claim is
parametrized over both. The central claim is append-only history: a write derived from anything
but the head is a `RevisionConflict`, never a silent overwrite.
"""

import asyncio
from collections.abc import Callable, Coroutine
from itertools import product
from typing import Any, TypeVar, get_args
from uuid import uuid4

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
    RevisionKind,
)
from chemclaw.protocols.store import (
    DesignStore,
    InMemoryDesignStore,
    PostgresDesignStore,
    RevisionConflict,
    StatusConflict,
    UnknownDesign,
    UnstorableDocument,
    advanced,
    default_design_store,
    require_movable,
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


def _fresh_id(backend: str, name: str) -> str:
    """A design id unique to this run, for tests that assert on a design's first revision.

    `_id` is stable so a Postgres row can be inspected after a failure; these tests must not see an
    earlier run's history.
    """
    return f"design-{backend}-{name}-{uuid4().hex[:8]}"


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
        await store.append(design_id, _design(), [], author_kind="agent")
        await store.append(design_id, _design(), [], author_kind="human", parent_revision=1)

        with pytest.raises(RevisionConflict, match="is at revision 2"):
            await store.append(design_id, _design(), [], author_kind="human", parent_revision=1)

        # And nothing was written by the refusal.
        assert [item.revision for item in await store.history(design_id)] == [1, 2]

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_parent_revision_zero_on_an_existing_design_is_refused(backend: str) -> None:
    """`0` means "I am creating this"; it is not a shortcut for "the head, whatever it is"."""

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "zero-parent")
        await store.append(design_id, _design(), [], author_kind="agent")

        with pytest.raises(RevisionConflict, match="derived from 0"):
            await store.append(design_id, _design(), [], author_kind="agent", parent_revision=0)

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
                author_kind="agent",
                status=status,
            )
            # The order the listing reports is `updated_at DESC`, and two appends inside one
            # millisecond would make that ordering a coin toss rather than a claim.
            await asyncio.sleep(0.01)

        mine = {row[0] for row in rows}
        listed = [s.design_id for s in (await store.listing()).designs if s.design_id in mine]
        assert listed == [rows[2][0], rows[1][0], rows[0][0]]

        drafts = {s.design_id for s in (await store.listing(status="draft")).designs} & mine
        assert drafts == {rows[1][0], rows[2][0]}

        project_a = {s.design_id for s in (await store.listing(project="prj-a")).designs} & mine
        assert project_a == {rows[0][0], rows[2][0]}

        both = [
            s.design_id
            for s in (await store.listing(status="draft", project="prj-a")).designs
            if s.design_id in mine
        ]
        assert both == [rows[2][0]]

        assert len((await store.listing(limit=1)).designs) == 1

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_set_status_on_an_unknown_design_is_refused(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        with pytest.raises(UnknownDesign, match="no design"):
            await store.set_status(
                _id(backend, "ghost"),
                "approved",
                expected_revision=1,
                expected_status="draft",
                actor="chemist-a",
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_set_status_moves_a_design_a_write_never_would(backend: str) -> None:
    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "status-move")
        # An arm, because `require_movable` refuses `approved` on a design holding only the ask —
        # and the revision's `kind` is derived from the document, so the two cannot disagree.
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        await store.set_status(
            design_id,
            "approved",
            expected_revision=1,
            expected_status="draft",
            actor="chemist-a",
        )
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_one_automatic_status_transition_and_the_two_that_are_not(backend: str) -> None:
    """Two transitions happen on a write; the rest are a human's.

    The first protocol revision makes a `requested` design a `draft`, and a revision landing on an
    `approved` design returns it to `draft`, since an approval is about a document that has changed.
    `abandoned` is held: a design someone decided not to run does not come back because an agent
    wrote to it.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "lifecycle")

        await store.append(design_id, _design(), [], author_kind="agent", status="requested")
        first = await store.summary(design_id)
        assert first is not None and first.status == "requested"

        # A revision that actually holds a procedure, because `kind` is derived from the document
        # rather than asserted beside it: this is the write that makes a `requested` design a
        # `draft`, and it is the *procedure arriving* that makes it one.
        await store.append(
            design_id,
            _design(arms=1),
            [],
            author_kind="agent",
            parent_revision=1,
            status="draft",
        )
        second = await store.summary(design_id)
        assert second is not None and second.status == "draft"

        await store.set_status(design_id, "approved", expected_revision=2, expected_status="draft")
        await store.append(
            design_id,
            _design(arms=1),
            [],
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

        await store.set_status(design_id, "abandoned", expected_revision=3, expected_status="draft")
        await store.append(
            design_id,
            _design(arms=1),
            [],
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
    """Every status `DesignStatus` allows is accepted by the schema's `CHECK` constraints.

    The constraints cannot be derived from the Literal, so all five statuses are driven through the
    real store. Two designs, because `require_movable` refuses `requested` on a protocol head; each
    status uses a design that may legally hold it.
    """

    async def _body() -> None:
        store = await _backend(backend)
        drafted_id = _id(backend, "allstatuses")
        drafted = _design(arms=1)
        await store.append(drafted_id, drafted, run_checks(drafted), author_kind="agent")
        ask_id = _id(backend, "allstatusesask")
        ask = _design(arms=0)
        await store.append(
            ask_id,
            ask,
            run_checks(ask, stage="request"),
            author_kind="agent",
            change_note="structured the request",
            status="requested",
        )
        # What each design currently holds, so every move can state the status it saw. Per design
        # rather than one value, because the ask exists to carry `requested` past the SQL constraint
        # and stays there while the drafted design walks the rest.
        held: dict[str, DesignStatus] = {drafted_id: "draft", ask_id: "requested"}
        for status in get_args(DesignStatus):
            design_id = ask_id if status == "requested" else drafted_id
            await store.set_status(
                design_id,
                status,
                expected_revision=1,
                expected_status=held[design_id],
                actor="chemist-a",
                reason=f"moving to {status}",
            )
            summary = await store.summary(design_id)
            assert summary is not None and summary.status == status
            held[design_id] = status

        on_a_protocol = [status for status in get_args(DesignStatus) if status != "requested"]
        recorded = [event.status for event in await store.status_history(drafted_id)]
        assert recorded == list(reversed(on_a_protocol))
        assert [event.status for event in await store.status_history(ask_id)] == ["requested"]

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
    """A status move records which revision it was made against.

    An approval is retired by the next revision, so only the recorded move answers which document
    was approved.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "signoff")
        design = _design(arms=2)
        await store.append(design_id, design, run_checks(design), author_kind="agent")
        await store.set_status(
            design_id,
            "approved",
            expected_revision=1,
            expected_status="draft",
            actor="chemist-a",
            reason="80 C is the precedent",
        )

        # The revision that un-approves it.
        await store.append(
            design_id,
            design,
            run_checks(design),
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
        await store.append(design_id, design, run_checks(design), author_kind="agent")
        assert await store.status_history(design_id) == []

        await store.set_status(
            design_id,
            "approved",
            expected_revision=1,
            expected_status="draft",
            actor="chemist-a",
            reason="fine",
        )
        await store.set_status(
            design_id,
            "executed",
            expected_revision=1,
            expected_status="approved",
            actor="chemist-b",
            reason="ran it Tuesday",
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
    """Two writers racing on one head: the loser gets `RevisionConflict`.

    Postgres only, since READ COMMITTED connections make the race possible; `asyncio.gather` over
    two real appends reproduces it. `_SELECT_HEAD`'s `FOR UPDATE` serialises them, so the loser
    fails the `parent_revision` comparison. This proves the contract (one winner, one revision 2),
    not the mechanism.
    """

    async def _body() -> None:
        await migrated_db_or_skip()
        store = PostgresDesignStore()
        design_id = _id("postgres", "race")
        await store.append(design_id, _design(), [], author_kind="agent")

        outcomes = await asyncio.gather(
            store.append(
                design_id,
                _design(arms=2),
                [],
                author_kind="human",
                parent_revision=1,
                change_note="alice, from revision 1",
            ),
            store.append(
                design_id,
                _design(arms=3),
                [],
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
    """`session_id` and `opened_by` are set once, by the write that opened the design, on both
    backends.

    `_UPSERT_DESIGN` omits them from `DO UPDATE SET`, and the in-memory store must match, or
    `listing(session_id=…)` differs by backend.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "session-owner")
        await store.append(
            design_id,
            _design(),
            [],
            author_kind="agent",
            author="chemist-a",
            session_id="one",
            status="requested",
        )
        await store.append(
            design_id,
            _design(arms=2),
            [],
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
        one = {row.design_id for row in (await store.listing(session_id="one")).designs}
        two = {row.design_id for row in (await store.listing(session_id="two")).designs}
        assert design_id in one
        assert design_id not in two

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_an_unpaired_surrogate_is_refused_rather_than_diverging(backend: str) -> None:
    r"""Both backends refuse an unpaired surrogate (`require_storable`).

    `json.loads` turns `"\\ud800"` into a lone surrogate and pydantic only refuses it on constrained
    strings, so Postgres would raise `UnicodeEncodeError` (a 500, not a `psycopg.Error`) while
    memory accepted it.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design = _design(arms=1)
        broken = design.model_copy(
            update={"base": design.base.model_copy(update={"waste": "quench \ud800"})}
        )
        with pytest.raises(UnstorableDocument, match="surrogate"):
            await store.append(
                _fresh_id(backend, "surrogate"),
                broken,
                run_checks(broken),
                author_kind="agent",
                author="chemist-a",
                change_note="drafted",
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_design_holding_only_the_ask_cannot_be_marked_executed(backend: str) -> None:
    """A lab record saying an experiment was run, over a document with no procedure in it.

    Nothing tied a status to the document it is a statement about, so `set_status("executed")` on a
    `request` revision was accepted on both backends.
    """

    async def _body() -> None:
        store = await _backend(backend)
        _askonly = _fresh_id(backend, "askonly")
        design = _design(arms=0)
        assert not design.has_protocol
        await store.append(
            _askonly,
            design,
            run_checks(design, stage="request"),
            author_kind="agent",
            author="chemist-a",
            change_note="structured the request",
            status="requested",
        )
        refused: DesignStatus
        for refused in ("executed", "approved"):
            with pytest.raises(UnstorableDocument, match="no procedure"):
                await store.set_status(
                    _askonly,
                    refused,
                    expected_revision=1,
                    expected_status="requested",
                    actor="chemist-a",
                    reason="ran it",
                )
        # `abandoned` says nothing about a procedure, so it stays available on an ask.
        await store.set_status(
            _askonly,
            "abandoned",
            expected_revision=1,
            expected_status="requested",
            actor="chemist-a",
            reason="not going ahead",
        )
        header = await store.summary(_askonly)
        assert header is not None and header.status == "abandoned"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_two_people_at_one_revision_cannot_both_decide(backend: str) -> None:
    """Two people at one revision cannot both decide.

    `expected_revision` covers the document, not the decision; `expected_status` closes that.
    Reading, thinking and clicking is enough, so this runs sequentially on both backends. The
    concurrent case is `test_two_deciders_racing_from_one_status_take_exactly_one_write`.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "two-deciders")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")

        await store.set_status(
            design_id,
            "abandoned",
            expected_revision=1,
            expected_status="draft",
            actor="alice",
            reason="the SM decomposes above 40 C",
        )
        with pytest.raises(StatusConflict, match="not 'draft' as you saw it"):
            await store.set_status(
                design_id,
                "approved",
                expected_revision=1,
                expected_status="draft",
                actor="bob",
                reason="looks fine to me",
            )

        header = await store.summary(design_id)
        assert header is not None and header.status == "abandoned", (
            "a design retired because the starting material decomposes must not come back into "
            "the draft listing because a second person had it open"
        )
        # And the refusal is not a quieter move: nothing was recorded either.
        assert [event.status for event in await store.status_history(design_id)] == ["abandoned"]

    _run(_body)


#: How many pairs the racing test drives. A row lock decides the outcome every time, so one round is
#: already evidence; repetition lets each decider be the winner, which the lock queue chooses.
_RACING_DECIDER_ROUNDS = 20


@pytest.mark.parametrize("backend", _BACKENDS)
def test_two_deciders_racing_from_one_status_take_exactly_one_write(backend: str) -> None:
    """Two people deciding at once from the same status: one write lands, the other is told.

    Asserts the count, never which decider wins (the lock queue's answer). The pair of moves is
    legal from `approved` and from each other's result, so only `require_unmoved` can refuse; that
    is asserted. Both backends: the in-memory store is consistent because nothing in `set_status`
    awaits between read and write. Fails if `require_unmoved` or `_SELECT_HEAD`'s `FOR UPDATE` is
    removed.
    """
    require_movable("draft", "abandoned", "protocol")
    require_movable("abandoned", "draft", "protocol")

    async def _body() -> None:
        store = await _backend(backend)
        landed = refused = 0
        for index in range(_RACING_DECIDER_ROUNDS):
            design_id = _fresh_id(backend, f"race-decide{index}")
            await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
            await store.set_status(
                design_id,
                "approved",
                expected_revision=1,
                expected_status="draft",
                actor="chemist-a",
                reason="approved on the bench",
            )

            # Alice sends it back for another arm; bob retires it. Both read `approved` and neither
            # sees the other.
            outcomes = await asyncio.gather(
                store.set_status(
                    design_id,
                    "draft",
                    expected_revision=1,
                    expected_status="approved",
                    actor="alice",
                    reason="one more arm before we run it",
                ),
                store.set_status(
                    design_id,
                    "abandoned",
                    expected_revision=1,
                    expected_status="approved",
                    actor="bob",
                    reason="the SM decomposes above 40 C",
                ),
                return_exceptions=True,
            )
            refusals = [item for item in outcomes if isinstance(item, BaseException)]
            assert len(refusals) == 1, (
                f"round {index}: {2 - len(refusals)} of the two writes landed; exactly one has to, "
                "or one of the two people was never told their decision was overwritten"
            )
            assert isinstance(refusals[0], StatusConflict), (
                f"round {index}: the loser was refused by something other than the status "
                f"compare-and-set ({refusals[0]!r})"
            )
            landed += sum(1 for item in outcomes if not isinstance(item, BaseException))
            refused += len(refusals)

            # And the record agrees with the header: the setup's sign-off plus the one move that
            # landed, newest first.
            header = await store.summary(design_id)
            events = await store.status_history(design_id)
            assert header is not None
            assert [event.status for event in events] == [header.status, "approved"], (
                f"round {index}: the header says {header.status!r} and the trail says "
                f"{[event.status for event in events]}"
            )

        # The count the store's own docstring quotes, reproduced: one write per pair and one
        # refusal per pair, summed over the run rather than asserted only round by round.
        assert (landed, refused) == (_RACING_DECIDER_ROUNDS, _RACING_DECIDER_ROUNDS)

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_the_status_a_caller_saw_is_the_one_it_moves_from(backend: str) -> None:
    """Bob is refused, re-reads, and decides again — the whole remedy, in one test.

    The complement of the refusal above, and the reason `require_unmoved` lets a no-op through:
    a caller that names the status actually on the design is exactly the caller that has read it.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "re-read")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        await store.set_status(
            design_id, "approved", expected_revision=1, expected_status="draft", actor="alice"
        )

        # Bob re-reads: the design is `approved`, and naming that is what lets him decide again.
        await store.set_status(
            design_id, "executed", expected_revision=1, expected_status="approved", actor="bob"
        )
        # A no-op move is not a conflict — a double click is not an error a chemist must interpret.
        await store.set_status(
            design_id, "executed", expected_revision=1, expected_status="executed", actor="bob"
        )

        header = await store.summary(design_id)
        assert header is not None and header.status == "executed"
        assert len(await store.status_history(design_id)) == 3

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_stale_revision_is_reported_before_a_stale_status(backend: str) -> None:
    """A stale revision is reported before a stale status.

    A revision landing on a decided design makes both stale; the document is the bigger loss and its
    remedy is a diff.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "both-stale")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        await store.set_status(
            design_id, "approved", expected_revision=1, expected_status="draft", actor="alice"
        )
        await store.append(
            design_id,
            _design(arms=2),
            [],
            author_kind="human",
            parent_revision=1,
            change_note="a second arm",
        )

        with pytest.raises(RevisionConflict, match="is not the head"):
            await store.set_status(
                design_id,
                "executed",
                expected_revision=1,
                expected_status="approved",
                actor="bob",
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_drafted_protocol_can_still_be_approved(backend: str) -> None:
    """The guard refuses an ask, never a protocol — the ordinary sign-off path."""

    async def _body() -> None:
        store = await _backend(backend)
        _signoff = _fresh_id(backend, "signoff")
        design = _design(arms=2)
        await store.append(
            _signoff,
            design,
            run_checks(design),
            author_kind="agent",
            author="chemist-a",
            change_note="drafted",
        )
        await store.set_status(
            _signoff,
            "approved",
            expected_revision=1,
            expected_status="draft",
            actor="chemist-a",
            reason="the precedent holds",
        )
        header = await store.summary(_signoff)
        assert header is not None and header.status == "approved"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_drafted_protocol_cannot_be_moved_back_to_requested(backend: str) -> None:
    """A drafted protocol cannot be moved back to `requested`.

    `requested` means the design holds only the ask, so a `protocol` head contradicts it, mirroring
    the refusal of `executed` on a request head.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _fresh_id(backend, "unrequest")
        design = _design(arms=2)
        await store.append(
            design_id,
            design,
            run_checks(design),
            author_kind="agent",
            author="chemist-a",
            change_note="drafted",
        )
        with pytest.raises(UnstorableDocument, match="holds a procedure"):
            await store.set_status(
                design_id,
                "requested",
                expected_revision=1,
                expected_status="draft",
                actor="chemist-a",
                reason="reopening the ask",
            )
        header = await store.summary(design_id)
        assert header is not None and header.status == "draft"

    _run(_body)


#: The lifecycle table as decided, written out rather than imported, so an accidental edit to the
#: store's map fails here. Self-transitions are one rule about retries and are not in the rows; they
#: are exempt from the table but not from the document rules.
_LEGAL_MOVES_AS_DECIDED: dict[str, set[str]] = {
    "requested": {"draft", "abandoned"},
    "draft": {"approved", "abandoned"},
    "approved": {"executed", "draft", "abandoned"},
    "executed": {"abandoned"},
    "abandoned": {"draft"},
}

#: The start states each head kind can hold: a `protocol` head cannot be `requested`, and a
#: `request` head cannot be `approved` or `executed`. Every (from, to) pair is driven through a real
#: store; for `approved -> requested` and `executed -> requested` the refusal is the document
#: rule's, which runs first, and the matrix asserts which pairs those are.
_PROTOCOL_HEAD_STATES: tuple[DesignStatus, ...] = ("draft", "approved", "executed", "abandoned")
_REQUEST_HEAD_STATES: tuple[DesignStatus, ...] = ("requested", "draft", "abandoned")


def _order_permits(current: str, target: str) -> bool:
    """What the decided table says about one move, self-transitions included."""
    return target == current or target in _LEGAL_MOVES_AS_DECIDED[current]


def _document_permits(target: str, head_kind: str) -> bool:
    """What the *other* half of `require_movable` says: a status against the head it describes."""
    if target in ("approved", "executed"):
        return head_kind == "protocol"
    if target == "requested":
        return head_kind == "request"
    return True


def _route_to(head_kind: str, state: DesignStatus) -> tuple[DesignStatus, ...]:
    """The moves that put a fresh design in `state` — every one of them legal under the table.

    A fixture that had to make an illegal move to set up its start state would be proving the
    absence of the guard while testing for its presence.
    """
    start = "draft" if head_kind == "protocol" else "requested"
    if state == start:
        return ()
    if state == "executed":
        return ("approved", "executed")
    return (state,)


def test_every_pair_of_statuses_is_decided_by_the_transition_table() -> None:
    """Every (from, to) status pair is decided by the transition table.

    Each pair runs on a head kind the document rule says nothing about, and the refusal message must
    name both statuses, so a document-rule refusal cannot pass as agreement.
    """
    for current in get_args(DesignStatus):
        for target in get_args(DesignStatus):
            head_kind: RevisionKind = "request" if target == "requested" else "protocol"
            if _order_permits(current, target):
                require_movable(current, target, head_kind)
                continue
            with pytest.raises(UnstorableDocument) as refusal:
                require_movable(current, target, head_kind)
            message = str(refusal.value)
            assert current in message and target in message, (
                f"the refusal of {current!r} -> {target!r} names neither where the design is nor "
                f"where it was asked to go: {message!r}"
            )


def test_a_repeat_is_exempt_from_the_table_and_not_from_the_document_rules() -> None:
    """A repeat is exempt from the table, not from the document rules.

    `requested` on a protocol head and `approved`/`executed` on a request head are refused even as
    repeats; the precedence is pinned for whoever adds a status. Expected pairs come from
    `_document_permits`.
    """
    for status in get_args(DesignStatus):
        for head_kind in ("request", "protocol"):
            if _document_permits(status, head_kind):
                require_movable(status, status, head_kind)
                continue
            with pytest.raises(UnstorableDocument) as refusal:
                require_movable(status, status, head_kind)
            assert status in str(refusal.value)


def test_the_transition_table_covers_every_status_the_type_allows() -> None:
    """The transition table covers every status the type allows.

    The table is indexed by current status, so an unknown status would be a `KeyError`; driving each
    self-transition forces the lookup for all five.
    """
    for status in get_args(DesignStatus):
        head_kind: RevisionKind = "request" if status == "requested" else "protocol"
        require_movable(status, status, head_kind)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_both_backends_decide_every_reachable_lifecycle_move_identically(backend: str) -> None:
    """Both backends decide every reachable lifecycle move identically, through `set_status`.

    The order rule reads current status from a dict on one side and a `FOR UPDATE` header row on the
    other. A refused move must leave the header unchanged. Coverage of the walk is asserted before
    it runs.
    """
    heads: tuple[tuple[RevisionKind, tuple[DesignStatus, ...], int], ...] = (
        ("protocol", _PROTOCOL_HEAD_STATES, 2),
        ("request", _REQUEST_HEAD_STATES, 0),
    )
    plan = [
        (head_kind, current, target, arms)
        for head_kind, states, arms in heads
        for current in states
        for target in get_args(DesignStatus)
    ]
    driven = {(current, target) for _, current, target, _ in plan}
    assert driven == set(product(get_args(DesignStatus), repeat=2)), (
        f"the walk drives {len(driven)} of the {len(get_args(DesignStatus)) ** 2} status pairs; "
        "a pair no store test reaches is decided by the pure-function test alone"
    )
    # Pairs whose store-side refusal is always the document rule's, so only the pure-function test
    # exercises their order refusal. Derived from the walk and compared to the decided list.
    document_only = {
        (current, target)
        for current, target in driven
        if all(
            not _document_permits(target, head_kind)
            for head_kind, walked_from, walked_to, _ in plan
            if (walked_from, walked_to) == (current, target)
        )
    }
    assert document_only == {
        ("requested", "approved"),
        ("requested", "executed"),
        ("approved", "requested"),
        ("executed", "requested"),
    }

    async def _body() -> None:
        store = await _backend(backend)
        for head_kind, current, target, arms in plan:
            design_id = _fresh_id(backend, f"move-{head_kind[:4]}-{current}-{target}")
            design = _design(arms=arms)
            await store.append(
                design_id,
                design,
                [],
                author_kind="agent",
                status="draft" if arms else "requested",
            )
            # `expected_status` is required, so the walk states what it is leaving; a
            # `StatusConflict` here would mean the fixture drifted, not that the table refused.
            seen: DesignStatus = "draft" if arms else "requested"
            for step in _route_to(head_kind, current):
                await store.set_status(design_id, step, expected_revision=1, expected_status=seen)
                seen = step
            where = f"{head_kind} head, {current} -> {target}"
            if _order_permits(current, target) and _document_permits(target, head_kind):
                await store.set_status(
                    design_id,
                    target,
                    expected_revision=1,
                    expected_status=seen,
                    actor="chemist-a",
                )
                summary = await store.summary(design_id)
                assert summary is not None and summary.status == target, where
                continue
            with pytest.raises(UnstorableDocument):
                await store.set_status(
                    design_id,
                    target,
                    expected_revision=1,
                    expected_status=seen,
                    actor="chemist-a",
                )
            summary = await store.summary(design_id)
            assert summary is not None and summary.status == current, (
                f"the move was refused and the header moved anyway ({where})"
            )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_repeated_sign_off_is_a_no_op_rather_than_a_refusal(backend: str) -> None:
    """A repeated sign-off is a no-op rather than a refusal, and its event row is still written.

    `Chemclaw3_ui` offers every status button, so `approved -> approved` is a co-signature or a
    second reason after a reload. The event table records who acted and why, so the repeat is
    recorded. A retry that has not re-read is a different case, refused earlier.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _fresh_id(backend, "retry")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        # Each click states what the panel showed; the second names `approved`, exercising
        # `require_unmoved` and the table's self-transition together.
        clicks: tuple[tuple[DesignStatus, str], ...] = (
            ("draft", "clicked approve"),
            ("approved", "approved again, with the plate in front of me"),
        )
        for seen, reason in clicks:
            await store.set_status(
                design_id,
                "approved",
                expected_revision=1,
                expected_status=seen,
                actor="chemist-a",
                reason=reason,
            )
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"
        assert [event.reason for event in await store.status_history(design_id)] == [
            "approved again, with the plate in front of me",
            "clicked approve",
        ]

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_retry_that_has_not_re_read_is_refused_before_the_repeat(backend: str) -> None:
    """A retry after a lost response, without a re-read, is refused by `require_unmoved`.

    The UI reloads only on success, so the retry sends the pre-move status and `require_movable` is
    never reached. The exception type says which guard answered: `StatusConflict` is
    `require_unmoved`'s, a table refusal would be `UnstorableDocument`.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _fresh_id(backend, "lost-response")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        await store.set_status(
            design_id,
            "approved",
            expected_revision=1,
            expected_status="draft",
            actor="chemist-a",
            reason="clicked approve",
        )

        # The response to that click never reached the browser, so the panel still reads `draft`
        # and the second click says so.
        with pytest.raises(StatusConflict, match="not 'draft' as you saw it"):
            await store.set_status(
                design_id,
                "approved",
                expected_revision=1,
                expected_status="draft",
                actor="chemist-a",
                reason="clicked approve again",
            )

        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"
        # The first click is recorded once and the retry adds nothing, which is the half a reader of
        # the old comment would have expected to be a second row.
        assert [event.reason for event in await store.status_history(design_id)] == [
            "clicked approve"
        ]

    _run(_body)


#: How many concurrent read/write rounds the torn-read test drives. See that test's docstring for
#: where the number comes from: the measured per-round tear rate is 4.5%, so this is the count at
#: which a broken store passes with probability ~0.01% instead of the 32% that 25 rounds gave.
_TORN_READ_ROUNDS = 200


def test_one_read_of_a_design_is_internally_consistent_under_a_concurrent_write() -> None:
    """One read of a design is internally consistent under a concurrent write.

    `page()` reads document, summary, history and status history in one `REPEATABLE READ`
    transaction; under READ COMMITTED each statement takes its own snapshot and can tear. Postgres
    only. The round count is chosen so a store that tears a few percent of the time fails with near
    certainty.
    """

    async def _body() -> None:
        await migrated_db_or_skip()
        store = PostgresDesignStore()
        torn = 0
        rounds = _TORN_READ_ROUNDS
        for index in range(rounds):
            design_id = _fresh_id("postgres", f"torn{index}")
            await store.append(design_id, _design(), [], author_kind="agent")

            page, _ = await asyncio.gather(
                store.page(design_id),
                store.append(
                    design_id,
                    _design(arms=2),
                    [],
                    author_kind="human",
                    parent_revision=1,
                    change_note="a colleague saves while this read is in flight",
                ),
                return_exceptions=True,
            )
            assert not isinstance(page, BaseException)
            assert page is not None
            assert page.summary is not None
            head_in_history = max(item.revision for item in page.history)
            if not (page.revision.revision == page.summary.head_revision == head_in_history):
                torn += 1
        assert torn == 0, f"{torn}/{rounds} reads disagreed with themselves"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_page_selects_a_revision_and_orders_the_two_histories(backend: str) -> None:
    """`page()` selects the right revision and orders both histories, on both backends.

    The torn-read test asserts only that the halves agree; this asserts the content (oldest-first
    history, newest-first sign-offs), and in-memory ordering says nothing about SQL.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _id(backend, "page-shape")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        await store.append(
            design_id,
            _design(arms=2),
            [],
            author_kind="human",
            parent_revision=1,
            change_note="a second arm",
        )
        await store.set_status(
            design_id,
            "approved",
            expected_revision=2,
            expected_status="draft",
            actor="chemist-a",
        )
        await store.set_status(
            design_id,
            "executed",
            expected_revision=2,
            expected_status="approved",
            actor="chemist-b",
        )

        head = await store.page(design_id)
        assert head is not None
        assert head.revision.revision == 2
        assert head.summary is not None and head.summary.head_revision == 2
        # Oldest first, which is the order a reviewer reads a design's history in.
        assert [item.revision for item in head.history] == [1, 2]
        assert [item.change_note for item in head.history] == ["", "a second arm"]
        # Newest first, because the last move is the one that describes the design now.
        assert [event.status for event in head.status_history] == ["executed", "approved"]
        assert [event.actor for event in head.status_history] == ["chemist-b", "chemist-a"]

        # An explicit revision serves that document, with the header and both histories still
        # describing the design as a whole rather than as it was.
        earlier = await store.page(design_id, 1)
        assert earlier is not None
        assert earlier.revision.revision == 1
        assert earlier.summary is not None and earlier.summary.head_revision == 2
        assert [item.revision for item in earlier.history] == [1, 2]

        assert await store.page(design_id, 3) is None
        # **There is no revision 0**, and the two backends disagreed about it: Postgres selected on
        # `(%s = 0 OR revision = %s)` over `revision or 0`, so a `revision=0` from a client got the
        # *head* there and `None` here — a divergence in the one method written to remove one.
        assert await store.page(design_id, 0) is None
        assert await store.page(_id(backend, "page-nothing")) is None

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_listing_says_how_many_designs_it_did_not_list(backend: str) -> None:
    """A listing says how many designs it did not list.

    Scoped to one project so other rows in the shared schema cannot distort the total.
    """

    async def _body() -> None:
        store = await _backend(backend)
        project = f"page-{backend}-{uuid4().hex[:8]}"
        for index in range(60):
            await store.append(
                _id(backend, f"page-{project}-{index:02d}"),
                _design(project=project),
                [],
                author_kind="agent",
            )

        page = await store.listing(project=project, limit=20)
        assert len(page.designs) == 20
        assert page.total == 60
        assert page.limit_applied == 20
        assert page.truncated

        # The clamp is visible as a clamp rather than as a corpus of 500.
        clamped = await store.listing(project=project, limit=10_000)
        assert clamped.limit_applied == 500
        assert not clamped.truncated

    _run(_body)
