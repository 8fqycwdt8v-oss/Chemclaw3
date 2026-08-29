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


async def _open(request_id: str, *, asked_of: str = "", days: float = 7.0) -> None:
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
