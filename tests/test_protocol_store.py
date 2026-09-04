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
    """A design id unique to this *run*, for the tests that must start from nothing.

    `_id` is stable so a Postgres row can be inspected after a failure; these three assert on a
    design's first revision, and a row left behind by an earlier run makes that assertion about
    somebody else's history.
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
        # An arm, because `require_movable` refuses `approved` on a design holding only the ask —
        # and the revision's `kind` is derived from the document, so the two cannot disagree.
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
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

        await store.set_status(design_id, "approved", expected_revision=2)
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

        await store.set_status(design_id, "abandoned", expected_revision=3)
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
    """Two `CHECK (status IN (...))` constraints now restate `DesignStatus`, in SQL.

    Neither can be derived from the Literal, so a sixth status added in Python would pass mypy,
    pass every in-memory test, and be rejected by Postgres at runtime — on the write, in front of a
    chemist. Driving all five through the real store is what ties the three declarations together.

    **Two designs, because `require_movable` now refuses one of the five on a protocol head.**
    `requested` means "holds only the structured ask", so a drafted design may not take it — the
    mirror of the refusal of `executed` on an ask. The subject here is the SQL constraint rather
    than the lifecycle, so each status is driven through a design that may legally hold it; a
    single-design version of this test would prove the constraint by breaking the guard.
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
        for status in get_args(DesignStatus):
            design_id = ask_id if status == "requested" else drafted_id
            await store.set_status(
                design_id,
                status,
                expected_revision=1,
                actor="chemist-a",
                reason=f"moving to {status}",
            )
            summary = await store.summary(design_id)
            assert summary is not None and summary.status == status

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
        await store.append(design_id, design, run_checks(design), author_kind="agent")
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
    a single-threaded dict cannot. No artificial barrier is needed and none is used:
    `asyncio.gather` over two real appends reproduces it, which is how it was found.

    **What decides it is `_SELECT_HEAD`'s `FOR UPDATE`, and this docstring used to name the primary
    key.** That was true when it was written — both writers read `head=1`, both built revision 2,
    and `(design_id, revision)` stopped the second as a raw `psycopg.errors.UniqueViolation` nobody
    translated, which is the 500 the 409 exists to prevent. The lock was then added for a different
    defect and serialises these two as a side effect, so the loser is refused by the
    `parent_revision` comparison and never reaches the INSERT: measured over 5x100 pairs, the
    primary key decided none of them, and replacing that handler with a raised `AssertionError`
    leaves the suite green.

    So what this test proves is the contract — exactly one writer wins, the loser is told
    `RevisionConflict`, and one revision 2 exists — and not the mechanism. The assertion that used
    to sit below, that the loser "cannot pass by inheritance" from `UniqueViolation`, was emphatic
    about a branch nothing reaches; it is gone rather than left looking load-bearing.
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
        assert design_id in {row.design_id for row in await store.listing(session_id="one")}
        assert design_id not in {row.design_id for row in await store.listing(session_id="two")}

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_an_unpaired_surrogate_is_refused_rather_than_diverging(backend: str) -> None:
    r"""Both backends refuse it, which is the only reason `require_storable` exists.

    Starlette parses a request body with stdlib `json.loads`, which turns `"\\ud800"` into a lone
    surrogate, and pydantic only refuses one on a `str` field carrying a constraint — so any
    unconstrained string in a design reached the driver. Measured on the real app before this:
    `POST /protocols/{id}/revisions` answered **500** on Postgres and **200** in memory. It is not
    even counted as a database failure, because `UnicodeEncodeError` is not a `psycopg.Error`.
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
                    actor="chemist-a",
                    reason="ran it",
                )
        # `abandoned` says nothing about a procedure, so it stays available on an ask.
        await store.set_status(
            _askonly,
            "abandoned",
            expected_revision=1,
            actor="chemist-a",
            reason="not going ahead",
        )
        header = await store.summary(_askonly)
        assert header is not None and header.status == "abandoned"

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
            actor="chemist-a",
            reason="the precedent holds",
        )
        header = await store.summary(_signoff)
        assert header is not None and header.status == "approved"

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_drafted_protocol_cannot_be_moved_back_to_requested(backend: str) -> None:
    """The mirror of the guard above, and it was missing while the guard's own argument covered it.

    `requested` is the one status that says the design "holds only a structured ask"
    (`models.DesignStatus`), so a `protocol` head contradicts it exactly as a `request` head
    contradicts `executed`. Nothing refused it: measured on both backends, an executed design moved
    to `requested` and stayed there with a fully drafted protocol as its head, so
    `GET /protocols?status=requested` listed it among the intakes and `?status=executed` did not.
    `advanced()` only repaired it when the *next* revision landed.
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
                actor="chemist-a",
                reason="reopening the ask",
            )
        header = await store.summary(design_id)
        assert header is not None and header.status == "draft"

    _run(_body)


#: The lifecycle table as the decision states it, written out here rather than imported from the
#: store. A test that reads the store's own map proves the code agrees with itself and nothing else;
#: this is the decided table, so a row edited by accident fails on this side.
#:
#: Every self-transition (`X -> X`) is legal on top of these and is deliberately *not* written into
#: the rows: it is one rule about retries rather than five decisions about the lifecycle.
_LEGAL_MOVES_AS_DECIDED: dict[str, set[str]] = {
    "requested": {"draft", "abandoned"},
    "draft": {"approved", "abandoned"},
    "approved": {"executed", "draft", "abandoned"},
    "executed": {"abandoned"},
    "abandoned": {"draft"},
}

#: The start states each head kind can actually hold. A `protocol` head cannot be `requested` (the
#: document rule refuses it) and a `request` head cannot be `approved` or `executed` (same rule,
#: read the other way), so the store matrix below drives 35 of the 25 pairs rather than all of them
#: — the two it cannot reach, `approved -> requested` and `executed -> requested`, are covered
#: against `require_movable` itself.
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
    """All 25 (from, to) pairs, driven on the head kind that leaves the *order* rule under test.

    `require_movable` answers two questions — is this status true of the document, and is this move
    legal from where the design is — so each pair here is driven on a head the first question has
    nothing to say about: `approved` and `executed` need a `protocol` head, `requested` needs a
    `request` one. Any refusal reaching these assertions is therefore the table's, and the message
    is checked for both status names so a document-rule refusal cannot pass as agreement.
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


def test_the_transition_table_covers_every_status_the_type_allows() -> None:
    """A sixth `DesignStatus` with no row in the table is a design nothing can move.

    The same tie `test_every_status_the_type_allows_is_a_status_the_schema_accepts` makes between
    the Literal and the two SQL `CHECK` constraints, one declaration along. The table is indexed on
    the *current* status, so a status the table does not know is a `KeyError` in front of a chemist
    — driving each status's self-transition is what forces that index for all five.
    """
    for status in get_args(DesignStatus):
        head_kind: RevisionKind = "request" if status == "requested" else "protocol"
        require_movable(status, status, head_kind)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_both_backends_decide_every_reachable_lifecycle_move_identically(backend: str) -> None:
    """The table as a matrix, through `set_status`, on a real store of each kind.

    A rule that holds on one backend and not the other is not a rule — `InMemoryDesignStore` is what
    a deployment without Postgres runs on — and the order rule needs the design's *current* status,
    which is read from a different place on each side: a dict here, the header row already locked
    `FOR UPDATE` there. Measured before this guard: `abandoned -> executed` and `draft -> executed`
    were both accepted on both backends, so a design retired because the starting material
    decomposes could be marked run, and a protocol nobody signed off could be marked run.

    A refused move must also leave the header where it was, which is the half a store could get
    wrong on its own: raising after the UPDATE would refuse the caller and move the design anyway.
    """

    async def _body() -> None:
        store = await _backend(backend)
        for head_kind, states, arms in (
            ("protocol", _PROTOCOL_HEAD_STATES, 2),
            ("request", _REQUEST_HEAD_STATES, 0),
        ):
            for current in states:
                for target in get_args(DesignStatus):
                    design_id = _fresh_id(backend, f"move-{head_kind[:4]}-{current}-{target}")
                    design = _design(arms=arms)
                    await store.append(
                        design_id,
                        design,
                        [],
                        author_kind="agent",
                        status="draft" if arms else "requested",
                    )
                    for step in _route_to(head_kind, current):
                        await store.set_status(design_id, step, expected_revision=1)
                    where = f"{head_kind} head, {current} -> {target}"
                    if _order_permits(current, target) and _document_permits(target, head_kind):
                        await store.set_status(
                            design_id, target, expected_revision=1, actor="chemist-a"
                        )
                        summary = await store.summary(design_id)
                        assert summary is not None and summary.status == target, where
                        continue
                    with pytest.raises(UnstorableDocument):
                        await store.set_status(
                            design_id, target, expected_revision=1, actor="chemist-a"
                        )
                    summary = await store.summary(design_id)
                    assert summary is not None and summary.status == current, (
                        f"the move was refused and the header moved anyway ({where})"
                    )

    _run(_body)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_repeated_sign_off_is_a_no_op_rather_than_a_refusal(backend: str) -> None:
    """Every `X -> X` is legal, and this is the reason the table permits them.

    `Chemclaw3_ui`'s sign-off panel renders a *Mark X* button for all five statuses whatever the
    design's current one is, so `approved -> approved` is one click away by construction — and a
    move whose response is lost is reported to the chemist as "The status was not recorded" when it
    may well have been, so pressing again is the ordinary recovery. Forbidding the repeat would turn
    a button the client offers into a 422 on the one screen where the answer is "it already worked".

    The event row is still written, which is the half worth asserting: a repeat is somebody acting a
    second time, and `experiment_protocol_status_events` is the record of who moved a design and
    why. A store that made the repeat a silent no-op would lose the second act.
    """

    async def _body() -> None:
        store = await _backend(backend)
        design_id = _fresh_id(backend, "retry")
        await store.append(design_id, _design(arms=1), [], author_kind="agent", status="draft")
        for reason in ("clicked approve", "clicked approve again"):
            await store.set_status(
                design_id, "approved", expected_revision=1, actor="chemist-a", reason=reason
            )
        summary = await store.summary(design_id)
        assert summary is not None and summary.status == "approved"
        assert [event.reason for event in await store.status_history(design_id)] == [
            "clicked approve again",
            "clicked approve",
        ]

    _run(_body)


#: How many concurrent read/write rounds the torn-read test drives. See that test's docstring for
#: where the number comes from: the measured per-round tear rate is 4.5%, so this is the count at
#: which a broken store passes with probability ~0.01% instead of the 32% that 25 rounds gave.
_TORN_READ_ROUNDS = 200


def test_one_read_of_a_design_is_internally_consistent_under_a_concurrent_write() -> None:
    """`GET /protocols/{id}` answers from one snapshot, not four.

    The route's own docstring says the history comes back in the same call because "asking for them
    separately makes the two answers race whenever somebody else is editing" — and the store then
    answered `read`, `summary`, `history` and `status_history` from four separate connections.
    Measured against a real database with one concurrent `append`: **100/100** reads were
    internally inconsistent, serving revision 1's document under a header saying head revision 2
    with revision 2 in the history beside it. A client picks its `parent_revision` out of that.

    One transaction is not by itself the fix, which is why this test is worth its cost: `core/db.py`
    is READ COMMITTED and takes a new snapshot per *statement*, so four statements in one
    transaction tear exactly as four transactions do. `page()` sets `REPEATABLE READ`.

    Postgres only — `InMemoryDesignStore` never yields between its four reads, so it has always
    been consistent, and every route test proved a property the deployment did not have.

    **The round count is arithmetic rather than a round number**, and the first one was too small
    to catch what it exists to catch. A race does not tear on every round: measured on this database
    with the isolation level removed and nothing else changed, **9 of 200** rounds tore — 4.5%. At
    the 25 rounds this ran for, a broken store passes with probability `0.955 ** 25`, which is
    **32%**: the test missed the regression roughly one run in three, and a green line was therefore
    not evidence. At 200 the same figure is `0.955 ** 200` ≈ 0.01%, for about five seconds more.
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
    """The four halves of `GET /protocols/{id}`, over both backends, on a design with a past.

    The torn-read test above drives `page()` hard and asserts only that its halves *agree*; nothing
    asserted what any of them contains. So the parts a client actually reads — which revision came
    back, whether the history is oldest-first, whether the sign-offs are newest-first — were
    unchecked on the backend that serves them, and the in-memory store is not evidence about SQL
    ordering.
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
        await store.set_status(design_id, "approved", expected_revision=2, actor="chemist-a")
        await store.set_status(design_id, "executed", expected_revision=2, actor="chemist-b")

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
