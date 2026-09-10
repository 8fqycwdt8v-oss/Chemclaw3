"""What `check_pending_requests` tells the model about how much it did not show.

The tool answers "have we already asked for this, and what is outstanding" — the same question the
chemistry-search surface answers about structures, and the surface that was never given the same
honesty. Measured against a real database: 35 waiting rows, 20 returned by the shipped default,
and no field, log line or counter naming the fifteen. Its docstring said "everything still waiting"
and its one caveat was about the *other* incompleteness (this system knows only its own questions),
so the warning present was the one that was not biting.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from chemclaw.agent.pending_tools import check_pending_requests
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import pending_store
from tests.pg import migrated_db_or_skip

ASKED_OF = "pending-tools-team"


async def _populate(count: int) -> None:
    """`count` open requests routed to this file's own entitlement, deadlines ascending.

    **Every waiting row goes first, not just this file's**, and that is a fact about the query
    rather than housekeeping. `open_requests(asked_of=X)` matches `asked_of = ANY([X]) OR asked_of
    = ''` on purpose — "an unrouted request is waiting on whoever is entitled, so hiding it from a
    named query would make the common case invisible" — so a *total* over that predicate counts
    every unrouted row in the schema too, whoever wrote it. Measured when this file first ran after
    `test_pending_store.py` in one session: 35 rows of its own, `total_waiting` **71**, the other
    36 unrouted rows belonging to a neighbouring file. The production behaviour is right and the
    fixture was scoped narrower than the thing it asserted about.

    Safe for the neighbours it clears: `test_pending_store.py::_clean` deletes its own rows and
    `test_api_pending.py::_open` deletes by request id, both *before* opening what each test needs,
    so neither depends on a row surviving from an earlier one.
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


def test_a_page_of_the_wait_says_it_is_a_page() -> None:
    """35 waiting, 20 shown — and the answer now carries both numbers and what they mean.

    The verdict is a `computed_field` rather than a property, so it survives `model_dump()`: the
    lesson `FingerprintSearch.verdict` records, learned on a hazard screen whose "this is not a
    safety assessment" sentence never left the process.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_nothing_waiting_is_said_as_nothing_waiting() -> None:
    """An empty page and a page that ran out are different answers and must read differently."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _populate(0)

        empty = await check_pending_requests(asked_of=ASKED_OF)
        assert empty.requests == []
        assert empty.total_waiting == 0
        assert "NOTHING WAITING" in empty.model_dump()["verdict"]

    asyncio.run(_run())
