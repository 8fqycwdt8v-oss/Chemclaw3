"""The projection behind the durable wait, driven against a real database.

The workflow is the authority on whether a wait is open; this table is what makes "what is waiting
on *me*" answerable, because Temporal can list its own runs and knows nothing about the subject
line, the requester or the reason.

The property worth the database is the **transition guard**. An expiry racing a person's click is
the ordinary case here, not an edge one — the deadline fires on a timer and the answer arrives from
a browser — and without `WHERE state = 'waiting'` in the SQL the outcome would be decided by
whichever transaction commits second. A guard in the worker would not do: the two writers are two
processes.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import pending_store
from tests.pg import migrated_db_or_skip

REQUESTER = "pending-test-requester"


async def _clean() -> None:
    """Remove this file's rows, so a re-run starts from the same place."""
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM pending_requests WHERE requested_by = %s", (REQUESTER,))
        await conn.commit()


async def _open(
    request_id: str, *, asked_of: str = "", days: float = 7.0, run_id: str = "run-1"
) -> None:
    """Open one wait with this file's requester."""
    await pending_store.open_request(
        request_id=request_id,
        kind="measurement",
        subject="run the four conditions",
        rationale="the campaign is suspended on this batch",
        asked_of=asked_of,
        requested_by=REQUESTER,
        session_id="s-1",
        correlation_id="c-1",
        due_at=datetime.now(UTC) + timedelta(days=days),
        run_id=run_id,
    )


def test_a_wait_can_be_settled_exactly_once() -> None:
    """The first writer wins; the second is told it did not settle, and the row is unchanged.

    The return value is the whole point. An expiry that silently no-ops looks identical to one that
    succeeded, so the workflow could not tell "somebody answered while I was timing out" from "I
    ended this", and the inbox and the outcome would disagree.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("pending-race")

        answered = await pending_store.settle_request(
            "pending-race", state="answered", answered_by="u-lab", answer={"yield": 0.7}
        )
        expired = await pending_store.settle_request(
            "pending-race", state="expired", answered_by="", answer={}
        )

        assert answered is True
        assert expired is False
        stored = await pending_store.get_request("pending-race")
        assert stored is not None
        assert stored.state == "answered"
        assert stored.answered_by == "u-lab"
        assert stored.answer == {"yield": 0.7}

    asyncio.run(_run())


def test_reopening_a_settled_request_does_nothing() -> None:
    """`open_request` is idempotent for a retry and inert for a decided wait.

    The activity that opens the projection runs at-least-once, so it must be replayable. It must
    also never resurrect a settled request: a retry arriving after somebody answered would put the
    question back in their inbox with the answer already recorded.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("pending-reopen")
        await pending_store.settle_request(
            "pending-reopen", state="answered", answered_by="u-1", answer={}
        )
        await _open("pending-reopen", days=99.0)

        stored = await pending_store.get_request("pending-reopen")
        assert stored is not None
        assert stored.state == "answered"

    asyncio.run(_run())


def test_the_inbox_shows_what_is_routed_to_you_and_what_is_routed_to_nobody() -> None:
    """An unrouted request is waiting on whoever is entitled, so it appears in a named query.

    Hiding it would make the common case invisible: a question raised without knowing the right
    name is the default, and an inbox that only showed personally-addressed rows would show
    almost nothing.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("pending-mine", asked_of="u-me", days=1)
        await _open("pending-anyone", asked_of="", days=2)
        await _open("pending-theirs", asked_of="u-them", days=3)

        mine = {row.request_id for row in await pending_store.open_requests(asked_of="u-me")}
        assert "pending-mine" in mine
        assert "pending-anyone" in mine
        assert "pending-theirs" not in mine

        # Unnarrowed, everything open is listed — the operator's view.
        everything = {row.request_id for row in await pending_store.open_requests()}
        assert {"pending-mine", "pending-anyone", "pending-theirs"} <= everything

    asyncio.run(_run())


def test_the_inbox_is_ordered_by_deadline_and_drops_what_is_settled() -> None:
    """Soonest first, and a settled request leaves the list."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("pending-late", asked_of="u-order", days=30)
        await _open("pending-soon", asked_of="u-order", days=1)

        order = [
            row.request_id
            for row in await pending_store.open_requests(asked_of="u-order")
            if row.requested_by == REQUESTER
        ]
        assert order == ["pending-soon", "pending-late"]

        await pending_store.settle_request(
            "pending-soon", state="answered", answered_by="u-order", answer={}
        )
        remaining = [
            row.request_id
            for row in await pending_store.open_requests(asked_of="u-order")
            if row.requested_by == REQUESTER
        ]
        assert remaining == ["pending-late"]

    asyncio.run(_run())


def test_a_reminder_counts_only_while_the_request_is_open() -> None:
    """Chasing a settled request is a no-op, so the count means what it says."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("pending-chase")
        await pending_store.record_reminder("pending-chase")
        await pending_store.record_reminder("pending-chase")
        await pending_store.settle_request(
            "pending-chase", state="expired", answered_by="", answer={}
        )
        await pending_store.record_reminder("pending-chase")

        stored = await pending_store.get_request("pending-chase")
        assert stored is not None
        assert stored.reminders == 2

    asyncio.run(_run())


def test_asking_again_after_a_deadline_lapsed_reopens_the_row() -> None:
    """The case `ALLOW_DUPLICATE` exists for, which the projection used to drop on the floor.

    `request_id_for` is deterministic, so a re-ask reuses the workflow id; `request_external_input`
    sets `WorkflowIDReusePolicy.ALLOW_DUPLICATE` precisely so a lapsed question can be asked again.
    The projection guarded its upsert on `state = 'waiting'` and never reset the state, so the new
    wait inherited the old cycle's `expired` row: invisible to `open_requests`, frozen for
    `record_reminder`, and refused 409 by the answer route — forever, while the workflow ran on.

    The run id is what separates a retry from a re-ask, so both halves are asserted here.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        request_id = "req-reask"
        await _clean()
        await _open(request_id, days=1, run_id="run-1")
        await pending_store.record_reminder(request_id)
        await pending_store.settle_request(request_id, state="expired", answered_by="", answer={})

        stored = await pending_store.get_request(request_id)
        assert stored is not None and stored.state == "expired"

        # The same question, asked again: a new Temporal run under the same workflow id.
        await _open(request_id, days=7, run_id="run-2")

        stored = await pending_store.get_request(request_id)
        assert stored is not None
        assert stored.state == "waiting", (
            "the re-asked question kept the lapsed cycle's state, so nobody can see or answer it"
        )
        assert not stored.answered_at and stored.answered_by == ""
        assert stored.reminders == 0, "the new cycle inherited the old one's chase count"
        assert [r.request_id for r in await pending_store.open_requests()] == [request_id]

    asyncio.run(_run())


def test_a_retry_of_the_opening_activity_does_not_disturb_a_settled_row() -> None:
    """The case the original guard was written for, which must survive the fix.

    An activity retry carries the *same* run. If that reopened a settled row, an at-least-once
    delivery could resurrect a wait the workflow had already answered — which is why the reopen is
    keyed on the run id changing rather than on the state alone.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        request_id = "req-retry"
        await _clean()
        await _open(request_id, days=1, run_id="run-1")
        await pending_store.settle_request(
            request_id, state="answered", answered_by="u-2", answer={"ok": True}
        )
        await _open(request_id, days=1, run_id="run-1")

        stored = await pending_store.get_request(request_id)
        assert stored is not None
        assert (stored.state, stored.answered_by) == ("answered", "u-2"), (
            "a retry of the opening activity resurrected a wait that was already answered"
        )

    asyncio.run(_run())


def test_an_expiry_does_not_claim_somebody_answered() -> None:
    """`answered_at` is a fact about a person, not about a state transition.

    It was stamped on every settle, so an `expired` row carried a timestamp beside an empty
    `answered_by` — the front door and the agent both read that as "somebody answered at some
    point". Migration 076's `pending_requests_answer_is_attributed` exists to prevent exactly that
    claim and only fires on `state = 'answered'`; the write walked around it from the other side.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean()
        await _open("req-expiry-stamp", days=1)
        await pending_store.settle_request(
            "req-expiry-stamp", state="expired", answered_by="", answer={}
        )
        stored = await pending_store.get_request("req-expiry-stamp")
        assert stored is not None
        assert stored.state == "expired"
        assert not stored.answered_at, "an unanswered request carries an answered-at timestamp"

        await _open("req-answered-stamp", days=1)
        await pending_store.settle_request(
            "req-answered-stamp", state="answered", answered_by="u-2", answer={"ok": True}
        )
        answered = await pending_store.get_request("req-answered-stamp")
        assert answered is not None and answered.answered_at, "a real answer lost its timestamp"

    asyncio.run(_run())
