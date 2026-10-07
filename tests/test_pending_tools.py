"""What `check_pending_requests` tells the model about how much it did not show.

A page of outstanding requests must say it is a page, or the model reports everything as
everything still waiting.
"""

from datetime import UTC, datetime, timedelta

from chemclaw.agent.pending_tools import check_pending_requests
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import pending_store
from tests.pg import migrated_db_or_skip

ASKED_OF = "pending-tools-team"


async def _populate(count: int) -> None:
    """`count` open requests routed to this file's own entitlement, deadlines ascending.

    Every waiting row is cleared first, not only this file's: a named query also matches unrouted
    rows (`asked_of = ''`), so a total would include other files' rows. Neighbours clear their own
    rows before each test, so this is safe for them.
    """
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM pending_requests WHERE state = 'waiting'")
        await conn.commit()
    for index in range(count):
        await pending_store.open_request(
            request_id=f"pending-tools-{index:02d}",
            kind="measurement",
            subject=f"run condition {index}",
            rationale="the campaign is suspended on this batch",
            asked_of=ASKED_OF,
            requested_by="pending-tools-requester",
            session_id="s-1",
            correlation_id="c-1",
            due_at=datetime.now(UTC) + timedelta(days=index + 1),
            run_id="r-1",
        )


async def test_a_page_of_the_wait_says_it_is_a_page() -> None:
    """35 waiting, 20 shown: the answer carries both numbers and what they mean.

    The verdict is a `computed_field`, so it survives `model_dump()`.
    """
    await migrated_db_or_skip()
    await _populate(35)

    page = await check_pending_requests(asked_of=ASKED_OF, limit=20)
    assert len(page.requests) == 20
    assert page.total_waiting == 35
    assert page.limit_applied == 20
    payload = page.model_dump()
    assert "PARTIAL" in payload["verdict"]
    assert "35" in payload["verdict"]

    # The marker means something, because the whole set does not set it.
    whole = await check_pending_requests(asked_of=ASKED_OF, limit=200)
    assert len(whole.requests) == 35
    assert "COMPLETE" in whole.model_dump()["verdict"]


async def test_nothing_waiting_is_said_as_nothing_waiting() -> None:
    """An empty page and a page that ran out are different answers and must read differently."""
    await migrated_db_or_skip()
    await _populate(0)

    empty = await check_pending_requests(asked_of=ASKED_OF)
    assert empty.requests == []
    assert empty.total_waiting == 0
    assert "NOTHING WAITING" in empty.model_dump()["verdict"]
