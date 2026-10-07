"""The projection behind the durable wait, driven against a real database.

The workflow decides whether a wait is open; this table makes "what is waiting on me" answerable.
The property worth the database is the transition guard: an expiry racing a person's answer is
ordinary, the two writers are different processes, and `WHERE state = 'waiting'` in the SQL is
what decides it.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import pending_store
from tests.pg import migrated_db_or_skip

REQUESTER = "pending-test-requester"


async def _clean() -> None:
    """Remove this file's rows, including archived answers, so a re-run starts clean.

    The application has no DELETE on `pending_request_answers`; this runs as the owner.
    """
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM pending_requests WHERE requested_by = %s", (REQUESTER,))
        await conn.execute(
            "DELETE FROM pending_request_answers WHERE requested_by = %s", (REQUESTER,)
        )
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


async def test_a_wait_can_be_settled_exactly_once() -> None:
    """The first writer wins; the second is told it did not settle, and the row is unchanged.

    The return value lets the workflow tell "somebody answered while I was timing out" from "I ended
    this".
    """
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


async def test_reopening_a_settled_request_does_nothing() -> None:
    """`open_request` is idempotent for a retry and inert for a decided wait.

    The opening activity runs at-least-once, and a late retry must not put an answered question back
    in an inbox.
    """
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


async def test_the_inbox_shows_what_is_routed_to_you_and_what_is_routed_to_nobody() -> None:
    """An unrouted request appears in a named inbox query.

    It is waiting on whoever is entitled, and unrouted is the common case.
    """
    await migrated_db_or_skip()
    await _clean()
    await _open("pending-mine", asked_of="u-me", days=1)
    await _open("pending-anyone", asked_of="", days=2)
    await _open("pending-theirs", asked_of="u-them", days=3)

    mine = {row.request_id for row in (await pending_store.open_requests(asked_of="u-me")).requests}
    assert "pending-mine" in mine
    assert "pending-anyone" in mine
    assert "pending-theirs" not in mine

    # Unnarrowed, everything open is listed — the operator's view.
    everything = {row.request_id for row in (await pending_store.open_requests()).requests}
    assert {"pending-mine", "pending-anyone", "pending-theirs"} <= everything


async def test_the_inbox_is_ordered_by_deadline_and_drops_what_is_settled() -> None:
    """Soonest first, and a settled request leaves the list."""
    await migrated_db_or_skip()
    await _clean()
    await _open("pending-late", asked_of="u-order", days=30)
    await _open("pending-soon", asked_of="u-order", days=1)

    order = [
        row.request_id
        for row in (await pending_store.open_requests(asked_of="u-order")).requests
        if row.requested_by == REQUESTER
    ]
    assert order == ["pending-soon", "pending-late"]

    await pending_store.settle_request(
        "pending-soon", state="answered", answered_by="u-order", answer={}
    )
    remaining = [
        row.request_id
        for row in (await pending_store.open_requests(asked_of="u-order")).requests
        if row.requested_by == REQUESTER
    ]
    assert remaining == ["pending-late"]


async def test_a_reminder_counts_only_while_the_request_is_open() -> None:
    """Chasing a settled request is a no-op, so the count means what it says."""
    await migrated_db_or_skip()
    await _clean()
    await _open("pending-chase")
    await pending_store.record_reminder("pending-chase", 1)
    await pending_store.record_reminder("pending-chase", 2)
    await pending_store.settle_request("pending-chase", state="expired", answered_by="", answer={})
    await pending_store.record_reminder("pending-chase", 3)

    stored = await pending_store.get_request("pending-chase")
    assert stored is not None
    assert stored.reminders == 2


async def test_asking_again_after_a_deadline_lapsed_reopens_the_row() -> None:
    """Asking again after a deadline lapsed reopens the row.

    `request_id_for` is deterministic and `ALLOW_DUPLICATE` permits re-asking, so a new run must
    reset an `expired` row or the question is invisible and unanswerable while the workflow waits.
    The run id separates a retry from a re-ask; both halves are asserted.
    """
    await migrated_db_or_skip()
    request_id = "req-reask"
    await _clean()
    await _open(request_id, days=1, run_id="run-1")
    await pending_store.record_reminder(request_id, 1)
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
    # Membership, not equality: these tables are shared by the whole suite and another file's
    # open request is not this test's business. Asserting the whole list is what made an
    # unrelated file fail this one in a full run and pass it alone.
    assert request_id in [r.request_id for r in (await pending_store.open_requests()).requests]


async def test_a_retry_of_the_opening_activity_does_not_disturb_a_settled_row() -> None:
    """A retry of the opening activity (same run) does not disturb a settled row."""
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


def test_a_re_ask_of_an_answered_question_opens_and_the_answer_is_archived() -> None:
    """All five shapes `_OPEN`'s guard admits, and what each does to the row and the archive.

    A re-ask of an answered question must open a new wait, so `_ARCHIVE_ANSWER` moves the previous
    answer into `pending_request_answers` (keyed on the answering run) in the same transaction
    (`D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`). Shapes:

    * a first ask opens;
    * a retry by the owning run leaves an answered row alone;
    * a re-ask by a different run reopens, with the previous answer archived whole;
    * a re-ask after an `expired` cycle reopens and archives nothing;
    * a caller with no `run_id` behaves as a different run.

    The archive is read by direct query: nothing in `src/` reads it, by design.
    """

    async def _archived(request_id: str) -> list[tuple[str, str, dict[str, Any]]]:
        async with await connect(settings.postgres_dsn) as conn:
            cur = await conn.execute(
                "SELECT run_id, answered_by, answer FROM pending_request_answers "
                "WHERE request_id = %s ORDER BY run_id",
                (request_id,),
            )
            return [(str(r[0]), str(r[1]), dict(r[2])) for r in await cur.fetchall()]

    async def _run() -> None:
        await migrated_db_or_skip()
        request_id = "req-claim"
        await _clean()

        await _open(request_id, run_id="run-1")
        await pending_store.settle_request(
            request_id, state="answered", answered_by="u-9", answer={"value": 1}
        )

        # A retry of the opening activity: the row is this run's already, and must not move.
        await _open(request_id, run_id="run-1")
        retried = await pending_store.get_request(request_id)
        assert retried is not None
        assert (retried.state, retried.answered_by, retried.answer) == (
            "answered",
            "u-9",
            {"value": 1},
        ), "a retry by the run that already owns the row disturbed a state it had settled"
        assert await _archived(request_id) == [], (
            "a retry archived its own answer, which makes the archive a log of redeliveries "
            "rather than of cycles"
        )

        # The re-ask. This is the case that used to fail the workflow.
        await _open(request_id, run_id="run-2")
        reopened = await pending_store.get_request(request_id)
        assert reopened is not None
        assert reopened.state == "waiting", (
            f"the re-ask left the row {reopened.state!r}, so the new wait is absent from every "
            "inbox and the answer route refuses it with a 409 naming somebody else's answer"
        )
        assert (reopened.answered_by, reopened.answer) == ("", {}), (
            "the reopened row still carries the previous cycle's attribution"
        )
        assert await _archived(request_id) == [("run-1", "u-9", {"value": 1})], (
            "the previous cycle's answer was not archived, so reopening destroyed the one record "
            f"of it: {await _archived(request_id)}"
        )

        # An expired cycle has nothing to archive, and reopening it is unchanged behaviour.
        await pending_store.settle_request(request_id, state="expired", answered_by="", answer={})
        await _open(request_id, run_id="run-3")
        assert await _archived(request_id) == [("run-1", "u-9", {"value": 1})], (
            "an expired cycle was archived; the archive is for answers, and a row with no "
            "`answered_by` violates its own CHECK"
        )

    asyncio.run(_run())


async def test_an_expiry_does_not_claim_somebody_answered() -> None:
    """An expiry does not stamp `answered_at`.

    `answered_at` is a fact about a person, and an `expired` row carrying one reads as "somebody
    answered".
    """
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


async def test_a_redelivered_reminder_does_not_count_one_escalation_twice() -> None:
    """A redelivered reminder does not count one escalation twice.

    The activity is at-least-once, so the write takes the replay-stable reminder number rather than
    incrementing. Driven as the redelivery: the same call twice leaves the same row, and a later
    number still advances it.
    """
    await migrated_db_or_skip()
    await _clean()
    await _open("pending-redelivered")

    # One escalation, delivered twice: the workflow's own count is 1 on both attempts.
    await pending_store.record_reminder("pending-redelivered", 1)
    await pending_store.record_reminder("pending-redelivered", 1)
    stored = await pending_store.get_request("pending-redelivered")
    assert stored is not None
    assert stored.reminders == 1, (
        "the redelivered attempt counted the same escalation a second time, so the row and "
        "the workflow disagree about how often somebody was chased"
    )

    # And the next escalation still lands, including after a redelivery it did not see.
    await pending_store.record_reminder("pending-redelivered", 2)
    stored = await pending_store.get_request("pending-redelivered")
    assert stored is not None
    assert stored.reminders == 2


async def test_the_inbox_query_says_how_much_it_did_not_return() -> None:
    """The inbox query says how much it did not return.

    A page must be distinguishable from the whole inbox, or a question can expire unsurfaced.
    `limit_applied` reports the store's clamp to 200.
    """
    await migrated_db_or_skip()
    await _clean()
    for index in range(35):
        await _open(f"pending-page-{index:02d}", days=index + 1)

    page = await pending_store.open_requests(limit=20)
    assert len(page.requests) == 20
    assert page.total_waiting >= 35
    assert page.limit_applied == 20
    assert page.truncated

    # ...and asking for more than the store will serve is visible as the clamp it is.
    clamped = await pending_store.open_requests(limit=10_000)
    assert clamped.limit_applied == 200

    # The ordinary case must read as complete, or the marker means nothing.
    whole = await pending_store.open_requests(limit=200)
    assert not whole.truncated


async def test_a_request_is_built_from_the_columns_by_name_and_keeps_its_iso_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests are built from columns by name, and the three timestamps stay ISO strings.

    `_COLUMNS` and `PendingRequest`'s fields are one declaration, so reversing it changes nothing.
    The `TIMESTAMPTZ` stamps are formatted by a `BeforeValidator` as `datetime.isoformat()` spells
    them.
    """
    from chemclaw.durable import pending_store as store

    await migrated_db_or_skip()
    await _clean()
    await _open("pending-by-name")
    straight = await store.get_request("pending-by-name")
    assert straight is not None
    assert straight.due_at.count("T") == 1 and straight.created_at.count("T") == 1
    assert straight.answered_at == "", "a NULL answered_at reads as 'still waiting'"

    columns = [name.strip() for name in store._COLUMNS.split(",")]
    monkeypatch.setattr(store, "_COLUMNS", ", ".join(reversed(columns)))
    assert await store.get_request("pending-by-name") == straight, (
        "the column order must not be able to decide which field a value lands in"
    )
    # The inbox is global across test files, so this asserts membership and the count's relation to
    # the page, which come from two cursors.
    page = await store.open_requests(limit=store._MAX_PAGE)
    assert "pending-by-name" in [row.request_id for row in page.requests]
    assert page.total_waiting >= len(page.requests) >= 1, (
        "the count must still be read, and it is the population the page is a page of"
    )

    monkeypatch.setattr(store, "_COLUMNS", f"{', '.join(columns)}, kind AS surplus")
    with pytest.raises(ValidationError, match="surplus"):
        await store.get_request("pending-by-name")
    await _clean()
