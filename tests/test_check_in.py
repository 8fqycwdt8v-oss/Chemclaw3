"""A requester hears their own work is still blocked, before the deadline rather than after it.

ADR: `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`.

`durable/awaiting.py` already re-notifies on a timer, and it notifies **`asked_of`** — the person
who has to do the thing. The requester is written to exactly once, on expiry. With
`awaiting_max_days` at 90 that is three months of silence about their own suspended campaign,
followed by a notice that it failed. This sweep is the notice in between.

Driven against the real query on a migrated database rather than against a fake grouping, because
the query *is* the feature: four predicates decide who hears what, and each one of them excludes a
population that would otherwise be told the wrong thing.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable import pending_store
from chemclaw.durable.check_in import CheckIn, _message, collect_check_ins
from tests.pg import migrated_db_or_skip

_OWNER = "check-in-alice"
_OTHER = "check-in-bob"


def _dsn() -> str:
    """The DSN the store itself resolves to, so the fixtures land in the same schema."""
    return settings.session_store_dsn or settings.postgres_dsn


async def _clear() -> None:
    """Drop this file's rows, so a re-run starts from nothing."""
    async with db.connection(_dsn()) as conn:
        await conn.execute(
            "DELETE FROM pending_requests WHERE requested_by = ANY(%s) OR request_id LIKE %s",
            ([_OWNER, _OTHER], "check-in-%"),
        )


async def _open(
    request_id: str,
    *,
    requested_by: str = _OWNER,
    opened_days_ago: float = 10.0,
    due_in_days: float = 5.0,
    subject: str = "run the four conditions from round 3",
) -> None:
    """One waiting request, aged by moving its own timestamps rather than by waiting."""
    await pending_store.open_request(
        request_id=request_id,
        kind="measurement",
        subject=subject,
        rationale="the Suzuki screen is suspended on it",
        asked_of="lab-team",
        requested_by=requested_by,
        session_id="s-1",
        correlation_id="c-1",
        due_at=datetime.now(UTC) + timedelta(days=due_in_days),
    )
    async with db.connection(_dsn()) as conn:
        await conn.execute(
            "UPDATE pending_requests SET created_at = now() - %s * INTERVAL '1 day'"
            " WHERE request_id = %s",
            (opened_days_ago, request_id),
        )


def _for(owner: str, items: list[CheckIn]) -> CheckIn | None:
    """This owner's check-in, or None if they were not told anything."""
    return next((item for item in items if item.owner == owner), None)


def test_a_quiet_question_reaches_the_person_who_asked_it() -> None:
    """The gap this closes: the requester, not the person it was asked of.

    `awaiting.py` re-notifies `asked_of` on `reminder_hours` and writes to `requested_by` only at
    expiry, so before this sweep a requester's only signal was the failure.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-quiet")

        mine = _for(_OWNER, await collect_check_ins())

        assert mine is not None, "the requester was told nothing about their own blocked work"
        (blocked,) = mine.requests
        assert blocked.request_id == "check-in-quiet"
        assert blocked.open_days >= 9, "the age is what makes 'still' mean something"
        assert 0 < blocked.days_left <= 5

    asyncio.run(_run())


def test_a_question_asked_this_morning_is_not_news(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below the quiet threshold nothing is said, or the second check-in teaches its reader to skip.

    The threshold is the whole reason this is a *check-in* rather than a second copy of the
    confirmation the requester already got when they asked.
    """
    monkeypatch.setattr(settings, "check_in_quiet_days", 3.0)

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-fresh", opened_days_ago=0.5)

        assert _for(_OWNER, await collect_check_ins()) is None

    asyncio.run(_run())


def test_an_expired_question_is_not_reported_twice() -> None:
    """Expiry already reaches the requester through the wait's own notice.

    Repeating it here would make this sweep a second, worse copy of a message that was already
    delivered — and a worse one, because it would arrive every night thereafter.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-expired", opened_days_ago=100.0, due_in_days=5.0)
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "UPDATE pending_requests SET due_at = now() - INTERVAL '1 day'"
                " WHERE request_id = %s",
                ("check-in-expired",),
            )

        assert _for(_OWNER, await collect_check_ins()) is None

    asyncio.run(_run())


def test_an_answered_question_stops_being_reported() -> None:
    """The obvious one, and the one whose absence would make this sweep nag forever."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-answered")
        await pending_store.settle_request(
            "check-in-answered", state="answered", answered_by="u-lab", answer={"yield": 0.7}
        )

        assert _for(_OWNER, await collect_check_ins()) is None

    asyncio.run(_run())


def test_each_requester_hears_only_their_own() -> None:
    """Grouping is by requester, and a leak here would show one chemist another's work."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-mine", requested_by=_OWNER)
        await _open("check-in-theirs", requested_by=_OTHER)

        items = await collect_check_ins()
        mine, theirs = _for(_OWNER, items), _for(_OTHER, items)

        assert mine is not None and theirs is not None
        assert [r.request_id for r in mine.requests] == ["check-in-mine"]
        assert [r.request_id for r in theirs.requests] == ["check-in-theirs"]

    asyncio.run(_run())


def test_a_request_with_no_requester_is_addressed_to_nobody_and_skipped() -> None:
    """A row with no actor cannot be reported *to* anyone, so it must not open an empty mailbox."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-actorless", requested_by="")

        items = await collect_check_ins()
        assert not any(item.owner == "" for item in items)

    asyncio.run(_run())


def test_the_message_carries_what_a_person_needs_to_act() -> None:
    """A channel reaches somebody with none of the context a surface has.

    So the outbound copy states the subject, who it is waiting on, how long, and the reason the
    requester themselves wrote — the same discipline `awaiting._awaiting_message` records.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-body")
        mine = _for(_OWNER, await collect_check_ins())
        assert mine is not None

        message = _message(mine)

        assert message.recipient == _OWNER
        assert "run the four conditions from round 3" in message.body
        assert "lab-team" in message.body, "say who it is waiting on"
        assert "the Suzuki screen is suspended on it" in message.body, "carry their own reason"
        assert "check-in-body" in message.body, "the id is the handle"

    asyncio.run(_run())


def test_the_sweep_runs_no_model() -> None:
    """An absence test, because "it interprets nothing" is the ADR's load-bearing claim.

    The richer version — an agent reading the blocked work and saying what it blocks — needs a
    `StepIdentity`, and a Schedule has none: synthesizing one from a `requested_by` string is this
    system granting itself a chemist's identity on a timer, for work that chemist did not ask for.
    A prose promise that this does not happen is worth what every unproducible claim in this tree
    has been worth, so it is asserted instead.
    """
    import ast
    from pathlib import Path

    # **Over the parsed tree, not the text.** The first draft of this test scanned
    # `source.split('\"\"\"')[2]` — the slice between the module docstring and the first class
    # docstring, measured at **18%** of the file — so a violation anywhere below it passed. Walking
    # the AST covers every statement and excludes docstrings for free, because a docstring is an
    # `ast.Constant` and carries no `Name`, `Attribute` or `alias`.
    tree = ast.parse(Path("src/chemclaw/durable/check_in.py").read_text(encoding="utf-8"))
    forbidden = {"run_agent_step", "AgentStepInput", "StepIdentity", "build_langgraph_agent"}
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.alias):
            named.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                named.add(node.asname)

    assert not (named & forbidden), (
        f"{sorted(named & forbidden)} appears in the check-in's code: this sweep must not run a "
        "model as a person who did not ask for it"
    )


def test_the_schedule_is_planned_only_when_a_deployment_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off by default, on when enabled — and named, so the pruner does not delete it.

    `OWNED_SCHEDULE_IDS` is the prune namespace: an id missing from it is a Schedule the applier is
    not authorised to remove, which strands it firing a workflow after a deployment turns the
    feature off.
    """
    from chemclaw.durable.schedules import OWNED_SCHEDULE_IDS, planned_schedules

    monkeypatch.setattr(settings, "check_in_enabled", False)
    assert "agent-check-in" not in {job.schedule_id for job in planned_schedules()}

    monkeypatch.setattr(settings, "check_in_enabled", True)
    assert "agent-check-in" in {job.schedule_id for job in planned_schedules()}
    assert "agent-check-in" in OWNED_SCHEDULE_IDS, "an unowned id can never be pruned"


def test_the_mailbox_the_sweep_writes_is_one_a_reader_can_open() -> None:
    """The round trip, and the defect it was written to catch was mine.

    `CHECK_IN_KIND` shipped with no reader: `GET /digests` claims `DIGEST_KIND` only, so a check-in
    would have landed in the mailbox nightly and nothing would ever have opened it — which is
    `D-2026-08-27-a-digest-nobody-can-read-is-not-delivered` a second time, in a commit whose own
    settings comment cited the ADR about it. Asserted end to end rather than at either half,
    because both halves passed their own tests while the feature delivered nothing.
    """
    from fastapi.testclient import TestClient

    from chemclaw.agent.session_events import claim_unconsumed, record_session_event
    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.durable.check_in import CHECK_IN_KIND
    from chemclaw.durable.digest import digest_channel

    async def _write() -> None:
        await migrated_db_or_skip()
        await claim_unconsumed(digest_channel(_OWNER))  # start clean
        await record_session_event(
            digest_channel(_OWNER),
            CHECK_IN_KIND,
            {
                "requests": [
                    {
                        "request_id": "check-in-wire",
                        "subject": "run the four conditions from round 3",
                        "rationale": "the Suzuki screen is suspended on it",
                        "asked_of": "lab-team",
                        "open_days": 9,
                        "days_left": 5,
                    }
                ]
            },
        )

    asyncio.run(_write())

    app = create_app(connector_factory=lambda _profile: [])
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid=_OWNER, upn=f"{_OWNER}@corp"
    )
    with TestClient(app) as client:
        first = client.get("/check-ins").json()
        second = client.get("/check-ins").json()

    assert [item["request_id"] for item in first] == ["check-in-wire"]
    assert first[0]["open_days"] == 9 and first[0]["days_left"] == 5
    assert first[0]["subject"] == "run the four conditions from round 3"
    assert second == [], "the read is the consume, so a second call must not re-deliver it"


def test_a_check_in_does_not_reach_another_chemists_mailbox() -> None:
    """The channel is derived from the authenticated principal, so there is nothing to authorize.

    Asserted anyway, because "nothing to authorize" is a claim about the derivation rather than a
    property nobody has to check — and the row must be left *untouched* for its owner, not merely
    filtered out of the wrong caller's answer.
    """
    from fastapi.testclient import TestClient

    from chemclaw.agent.session_events import record_session_event
    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.durable.check_in import CHECK_IN_KIND
    from chemclaw.durable.digest import digest_channel

    async def _write() -> None:
        await migrated_db_or_skip()
        # Deleted rather than claimed: `claim_unconsumed` leaves the rows it consumed in place, so
        # a neighbour test's already-consumed row would sit in this channel and the "untouched"
        # assertion below would read its `consumed_at` as this test's doing. Found by running the
        # file rather than the test.
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "DELETE FROM session_events WHERE session_id = ANY(%s)",
                ([digest_channel(_OWNER), digest_channel(_OTHER)],),
            )
        await record_session_event(
            digest_channel(_OWNER), CHECK_IN_KIND, {"requests": [{"request_id": "check-in-mine"}]}
        )

    asyncio.run(_write())

    app = create_app(connector_factory=lambda _profile: [])
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid=_OTHER, upn=f"{_OTHER}@corp"
    )
    with TestClient(app) as client:
        assert client.get("/check-ins").json() == [], "another chemist's check-in was served"

    async def _untouched() -> None:
        async with db.connection(_dsn()) as conn:
            cursor = await conn.execute(
                "SELECT consumed_at FROM session_events WHERE session_id = %s",
                (digest_channel(_OWNER),),
            )
            assert [row[0] for row in await cursor.fetchall()] == [None], (
                "the owner's row was consumed by somebody else's read"
            )

    asyncio.run(_untouched())
