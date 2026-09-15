"""A requester hears their own work is still blocked, before the deadline rather than after it.

ADR: `D-2026-09-15-the-requester-hears-nothing-until-it-is-too-late`.

`durable/awaiting.py` already re-notifies on a timer, and it notifies **`asked_of`** — the person
who has to do the thing. The requester is written to exactly once, on expiry. With
`awaiting_max_days` at 90 that is three months of silence about their own suspended campaign,
followed by a notice that it failed. This sweep is the notice in between.

Driven against the real query on a migrated database rather than against a fake grouping, because
the query *is* the feature: four predicates decide who hears what, and each one of them excludes a
population that would otherwise be told the wrong thing.

**And against the real workflow, because nothing here used to run it.** Every test below the query
ones drives `CheckInWorkflow` on a broker: the loop, the `delivered` count, the notify-then-deliver
ordering, the replay guard on the metric, the mailbox payload's own shape and the kind its outbound
copy travels under were each asserted at one end or the other and none of them end to end — so
renaming `BlockedRequest.open_days` left the suite green while `GET /check-ins` answered 0 for every
entry, and the hand-typed literal dict that stood in for the writer agreed with nothing.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.deliver.message import Message
from chemclaw.durable import pending_store
from chemclaw.durable.check_in import (
    _CONCURRENT_REQUESTERS,
    _MAX_TEXT_CHARS,
    _PAGE_ROWS,
    CHECK_IN_KIND,
    BlockedRequest,
    CheckIn,
    CheckInPage,
    CheckInWorkflow,
    _message,
    collect_check_ins,
    supersede_unread_check_ins,
)
from chemclaw.durable.deliver_message import OutboundMessage, deliver_message_activity
from chemclaw.durable.digest import digest_channel
from chemclaw.durable.notify import SessionEventInput, record_session_event_activity
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_env_or_skip

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


async def _collected() -> list[CheckIn]:
    """Every page the activity serves, walked the way the workflow walks them.

    The activity is paged (`D2`), so a test that read one page would be asserting about a prefix and
    calling it the population — which is the shape of the defect paging exists to fix. The loop also
    pins the termination condition: `more` false, or a cursor that failed to advance.
    """
    items: list[CheckIn] = []
    after = ""
    while True:
        page = await collect_check_ins(after)
        items.extend(page.check_ins)
        if not page.more or page.after <= after:
            return items
        after = page.after


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

        mine = _for(_OWNER, await _collected())

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

        assert _for(_OWNER, await _collected()) is None

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

        assert _for(_OWNER, await _collected()) is None

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

        assert _for(_OWNER, await _collected()) is None

    asyncio.run(_run())


def test_each_requester_hears_only_their_own() -> None:
    """Grouping is by requester, and a leak here would show one chemist another's work."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-mine", requested_by=_OWNER)
        await _open("check-in-theirs", requested_by=_OTHER)

        items = await _collected()
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

        items = await _collected()
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
        mine = _for(_OWNER, await _collected())
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
    from chemclaw.durable.digest import digest_channel

    async def _write() -> None:
        await migrated_db_or_skip()
        await claim_unconsumed(digest_channel(_OWNER))  # start clean
        # **Built by the writer's own model, not typed out.** The literal dict this replaced agreed
        # with `BlockedRequest` on the day it was written and with nothing afterwards: renaming
        # `open_days` would have left this test green while every entry `GET /check-ins` served read
        # 0, which is the one number the notice exists to carry.
        await record_session_event(
            digest_channel(_OWNER),
            CHECK_IN_KIND,
            {
                "requests": [
                    BlockedRequest(
                        request_id="check-in-wire",
                        kind="measurement",
                        subject="run the four conditions from round 3",
                        rationale="the Suzuki screen is suspended on it",
                        asked_of="lab-team",
                        open_days=9,
                        days_left=5,
                    ).model_dump()
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


def test_a_deadline_is_floored_rather_than_rounded() -> None:
    """`::int` rounds, so 4.6 days left arrived as "5 left" — half a day of borrowed deadline.

    The direction is what makes it worth a test rather than a shrug: over-stating how long is left
    makes a requester act later than they can afford to, and the whole point of the notice is that
    it reaches them *before* the deadline. `FLOOR` is the conservative side of the same arithmetic.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-floor", opened_days_ago=9.6, due_in_days=4.6)

        mine = _for(_OWNER, await _collected())
        assert mine is not None
        (blocked,) = mine.requests

        assert blocked.days_left == 4, (
            f"4.6 days left was reported as {blocked.days_left}; rounding a deadline up hands the "
            "requester time the request does not have"
        )
        assert blocked.open_days == 9, f"9.6 days open was reported as {blocked.open_days}"

    asyncio.run(_run())


def test_the_outbound_copy_is_not_delivered_as_a_digest() -> None:
    """The ADR's central claim, on the half a chemist who closed the tab actually receives.

    `_message` built its `OutboundMessage` with no `kind=`, so every check-in took the default
    `"digest"`: `FileDeliveryDriver` names files `<kind>-<identity>`, so it landed in the
    standing-query digest's own outbox file, and the webhook driver folds the kind into its
    `Idempotency-Key`. The mailbox honoured the distinction (`CHECK_IN_KIND`) and the channel did
    not, which is the one place the two notices are genuinely indistinguishable to a reader.
    """
    item = CheckIn(
        owner=_OWNER,
        requests=[BlockedRequest(request_id="check-in-kind", kind="measurement", subject="s")],
    )

    assert _message(item).kind == CHECK_IN_KIND, (
        "a check-in delivered as a digest is a check-in a surface cannot tell from a digest"
    )
    assert CHECK_IN_KIND in get_args(Message.model_fields["kind"].annotation), (
        "the kind has to be in `Message`'s Literal or the activity refuses it and counts a "
        "degradation instead of delivering"
    )


async def _open_many(requesters: dict[str, int], *, text_chars: int = 40) -> None:
    """`{requester: how many waiting questions}`, inserted in one statement rather than one each.

    Bulk because the population this exists to bound is thousands of rows and a per-row
    `open_request` round trip would make the bound untestable at the size it bites.
    """
    rows = [
        (
            f"check-in-bulk-{owner}-{index}",
            "measurement",
            "s" * text_chars,
            "r" * text_chars,
            "lab-team",
            owner,
            "s-1",
            "c-1",
            [],
        )
        for owner, count in requesters.items()
        for index in range(count)
    ]
    async with db.connection(_dsn()) as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO pending_requests (request_id, kind, subject, rationale, asked_of,"
                " requested_by, session_id, correlation_id, premise_note_ids, due_at, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " now() + INTERVAL '30 days', now() - INTERVAL '10 days')",
                rows,
            )


def test_a_page_stays_inside_the_blob_limit_and_the_walk_still_sees_everybody() -> None:
    """The most severe one: an unbounded activity result fails the sweep for ever at ~7,600 rows.

    Measured end to end on the live broker at 10,000 rows before the fix: `ServerError: Complete
    result exceeds size limit`, non-retryable, the run dead in 0.3 s and dead again every night
    after it — zero requesters told anything, which is precisely what the sweep exists to prevent,
    reproduced by its own scaling. With ~1 kB of model-authored text per request the crossover is
    ~2,000 rows, and `pending_requests` caps neither `subject` nor `rationale`.

    Driven at a size that spans three pages, and asserted on the three properties that make paging
    correct rather than merely present: every page fits, every requester is reached exactly once,
    and no requester is split across two pages — a split would be two notices each claiming to be
    the whole of somebody's blocked work.
    """
    #: Temporal's own gRPC payload ceiling, which is what the failure above is.
    blob_limit = 2 * 1024 * 1024
    population = {"check-in-p1": 150, "check-in-p2": 150, "check-in-p3": 5}

    async def _run() -> tuple[list[int], list[list[str]]]:
        await migrated_db_or_skip()
        await _clear()
        await _open_many(population)
        sizes: list[int] = []
        owners_by_page: list[list[str]] = []
        after = ""
        while True:
            page = await collect_check_ins(after)
            sizes.append(len(page.model_dump_json().encode()))
            owners_by_page.append([item.owner for item in page.check_ins])
            for item in page.check_ins:
                assert not item.truncated, (
                    f"{item.owner} has {len(item.requests)} of {population[item.owner]} questions "
                    "but was not split: a page boundary must fall between requesters"
                )
                assert len(item.requests) == population[item.owner], (
                    f"{item.owner} was served {len(item.requests)} of {population[item.owner]}"
                )
            if not page.more:
                return sizes, owners_by_page
            assert page.after > after, "the cursor must advance or the workflow loops for ever"
            after = page.after

    sizes, owners_by_page = asyncio.run(_run())
    flat = [owner for page in owners_by_page for owner in page]

    assert sorted(flat) == sorted(population), f"the walk reached {flat}"
    assert len(flat) == len(set(flat)), f"a requester was served twice: {flat}"
    assert len(owners_by_page) > 1, f"{sum(population.values())} rows fitted in one page: {sizes}"
    assert max(sizes) < blob_limit, f"a page serialized to {max(sizes)} bytes, over Temporal's cap"


def test_one_requester_who_fills_a_page_alone_is_truncated_and_says_so() -> None:
    """The one case the requester boundary cannot honour, and it must not stall the walk.

    Dropping the last requester in a full page is what keeps anybody from being split — but when
    they are the *only* requester in that page, dropping them leaves the cursor where it was and the
    workflow re-reads the same page for ever. So they are carried short, `truncated` says so, and
    the outbound copy says so in words: a silent truncation reads as completeness, which is
    `kg/conflicts.py`'s rule and the reason a short list is trustworthy here.
    """

    async def _run() -> tuple[CheckIn, CheckIn]:
        await migrated_db_or_skip()
        await _clear()
        await _open_many({"check-in-q1": _PAGE_ROWS + 60, "check-in-q2": 5})
        items = await _collected()
        big = _for("check-in-q1", items)
        small = _for("check-in-q2", items)
        assert big is not None and small is not None
        return big, small

    big, small = asyncio.run(_run())

    assert len(big.requests) == _PAGE_ROWS, f"the page carried {len(big.requests)} rows"
    assert big.truncated, "a requester served short must say so"
    assert "more waiting than one check-in carries" in _message(big).body
    assert not small.truncated and len(small.requests) == 5, (
        "the walk stalled on the oversized requester instead of advancing past them"
    )


def test_a_rationale_longer_than_the_bound_is_cut_and_names_what_it_cut() -> None:
    """A row cap is not a byte cap while the text is unbounded — and both fields are.

    `subject` and `rationale` are model-authored and `pending_requests` declares no length on
    either, so one oversized rationale defeats any number of rows. Cut in the query, and named in
    the text the reader sees rather than trimmed in silence.
    """
    overshoot = 250

    async def _run() -> BlockedRequest:
        await migrated_db_or_skip()
        await _clear()
        await _open_many({_OWNER: 1}, text_chars=_MAX_TEXT_CHARS + overshoot)
        mine = _for(_OWNER, await _collected())
        assert mine is not None
        return mine.requests[0]

    blocked = asyncio.run(_run())

    assert blocked.rationale.startswith("r" * _MAX_TEXT_CHARS)
    assert f"{overshoot} more character(s) not carried" in blocked.rationale
    assert f"{overshoot} more character(s) not carried" in blocked.subject


def test_a_junk_day_count_costs_one_field_and_not_the_whole_notice() -> None:
    """`_check_in`'s docstring promised totality and its two `int(...)` conversions were not total.

    The claim it makes is load-bearing: by the time the mapper runs, `claim_unconsumed` has already
    marked the row consumed, so a raise does not defer the notice — it destroys it, along with every
    other row claimed in the same batch, and there is nothing to re-find afterwards. Measured
    against this route before the fix: a text `open_days` raised `ValueError` and a dict raised
    `TypeError`, each a 500 with the row already gone. The sibling `_digest` is total on the same
    junk, which is what made the asymmetry easy to miss.
    """
    from chemclaw.api.routes.streams import _check_in

    junk = {
        "requests": [
            {"request_id": "r-text", "open_days": "many", "days_left": "soon"},
            {"request_id": "r-dict", "open_days": {}, "days_left": []},
            {"request_id": "r-none", "open_days": None, "days_left": None},
            {"request_id": "r-bool", "open_days": True, "days_left": False},
            {"request_id": "r-neg", "open_days": -3, "days_left": -1},
            {"request_id": "r-good", "open_days": 9, "days_left": 5},
        ]
    }

    read = _check_in(junk)

    assert [one.request_id for one in read] == [
        "r-text",
        "r-dict",
        "r-none",
        "r-bool",
        "r-neg",
        "r-good",
    ], "one unreadable entry must not take the rest of the notice with it"
    assert [(one.open_days, one.days_left) for one in read[:-1]] == [(0, 0)] * 5, (
        "an unreadable count reads as zero; `True` is not a day count and a negative one is corrupt"
    )
    assert (read[-1].open_days, read[-1].days_left) == (9, 5), "a good row still reads"


@asynccontextmanager
async def _sweep_worker(
    client: Client, activities: Sequence[Callable[..., Any]] | None = None
) -> AsyncIterator[None]:
    """A worker serving `CheckInWorkflow` and, by default, its four **real** activities.

    Real rather than faked, because the three defects the end-to-end tests below were written for
    all live in the seam between the workflow and its activities — the mailbox payload's shape, the
    kind its outbound copy travels under, and what the previous night left behind. A fake collector
    would have agreed with whatever the workflow expected, which is how this file came to have a
    "mailbox end to end" test that never called the writer.

    Unsandboxed for speed: the sandbox re-imports `durable/check_in.py` (and its transitive
    `chemclaw.core.db`) on every workflow task, and nothing here is testing the sandbox.
    """
    async with Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=[CheckInWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        activities=list(
            activities
            if activities is not None
            else [
                supersede_unread_check_ins,
                collect_check_ins,
                record_session_event_activity,
                deliver_message_activity,
            ]
        ),
    ):
        yield


async def _sweep(client: Client, suffix: str) -> int:
    """Run one whole sweep and return how many requesters it told."""
    return int(
        await client.execute_workflow(
            CheckInWorkflow.run,
            id=f"check-in-test-{suffix}",
            task_queue=settings.background_task_queue,
        )
    )


def test_the_sweep_itself_writes_the_mailbox_a_reader_can_open() -> None:
    """`CheckInWorkflow.run` was executed by nothing in this suite, so nothing joined the two ends.

    The loop, the `delivered` count, the notify-then-deliver ordering, the metric and the kind its
    outbound copy travels under were each asserted at one end or the other — and the "end to end"
    mailbox test wrote a hand-typed literal dict of its own. Renaming `BlockedRequest.open_days`
    left the whole file green while `GET /check-ins` answered `0` for every entry.

    This drives the real workflow against the real activities on the real broker, and reads what it
    wrote back out through the route a chemist's surface calls.
    """
    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal

    async def _run() -> int:
        await migrated_db_or_skip()
        await _clear()
        await _open("check-in-e2e")
        await supersede_unread_check_ins([_OWNER])
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with _sweep_worker(client):
                return await _sweep(client, "mailbox")

    delivered = asyncio.run(_run())
    assert delivered >= 1, "the sweep reported telling nobody"

    app = create_app(connector_factory=lambda _profile: [])
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid=_OWNER, upn=f"{_OWNER}@corp"
    )
    with TestClient(app) as client:
        served = client.get("/check-ins").json()

    mine = [one for one in served if one["request_id"] == "check-in-e2e"]
    assert mine, f"the sweep's own row did not survive the round trip: {served}"
    assert mine[0]["subject"] == "run the four conditions from round 3"
    assert mine[0]["rationale"] == "the Suzuki screen is suspended on it"
    assert mine[0]["asked_of"] == "lab-team"
    assert mine[0]["open_days"] >= 9 and 0 < mine[0]["days_left"] <= 5, (
        "the day counts the notice exists to carry arrived as zero"
    )


def test_a_second_night_replaces_the_first_and_a_read_check_in_becomes_prunable() -> None:
    """Nightly for ever into a mailbox retention can never empty, was the shape of it.

    There was no watermark of any kind, so a request open the full `awaiting_max_days` wrote ~87
    consecutive rows per requester, each differing by one integer — the habituation
    `check_in_quiet_days` exists to prevent, one layer down. And retention's `session_events`
    predicate is `consumed_at IS NOT NULL`, so until a surface calls `GET /check-ins` every one of
    them is unread for ever.

    In the shape of `test_digest.py`'s "a read digest becomes prunable and an unread one does not",
    and asserting the half that test cannot: the *population* is one row per requester however many
    nights run, and reading is still what makes the survivor disposable.
    """
    from chemclaw.durable.retention import prune_expired_rows

    async def _unread() -> list[object]:
        async with db.connection(_dsn()) as conn:
            cursor = await conn.execute(
                "SELECT consumed_at FROM session_events WHERE session_id = %s AND kind = %s",
                (digest_channel(_OWNER), CHECK_IN_KIND),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def _run() -> tuple[list[object], list[object]]:
        await migrated_db_or_skip()
        await _clear()
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "DELETE FROM session_events WHERE session_id = %s", (digest_channel(_OWNER),)
            )
        await _open("check-in-nightly")
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with _sweep_worker(client):
                for night in range(3):
                    await _sweep(client, f"nightly-{night}")
        return await _unread(), []

    async def _prune() -> list[object]:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_events_days", 7)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_tool_results_days", 0)
        monkeypatch.setattr(settings, "retention_checkpoints_days", 0)
        try:
            async with db.connection(_dsn()) as conn:
                await conn.execute(
                    "UPDATE session_events SET created_at = now() - make_interval(days => 90)"
                    " WHERE session_id = %s",
                    (digest_channel(_OWNER),),
                )
            await prune_expired_rows()
            return await _unread()
        finally:
            monkeypatch.undo()

    after_three_nights, _ = asyncio.run(_run())
    assert after_three_nights == [None], (
        f"three nights left {len(after_three_nights)} unread check-in(s) in one mailbox; each is "
        "the same question with one integer changed, and none of them can ever be pruned"
    )

    from fastapi.testclient import TestClient

    from chemclaw.api.app import create_app
    from chemclaw.api.auth import Principal, require_principal

    app = create_app(connector_factory=lambda _profile: [])
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid=_OWNER, upn=f"{_OWNER}@corp"
    )
    with TestClient(app) as client:
        assert len(client.get("/check-ins").json()) == 1

    assert asyncio.run(_prune()) == [], "a check-in the owner read was still not prunable"


def test_a_consumed_check_in_is_left_for_retention_rather_than_deleted() -> None:
    """The supersede is scoped to the *unread*, so it cannot destroy what a chemist already claimed.

    `session_events.consumed_at` is the evidence the runbook has operators read for this mailbox,
    and a delete that ignored it would erase the record that anything was ever delivered.
    """
    from chemclaw.agent.session_events import record_session_event

    async def _run() -> list[object]:
        await migrated_db_or_skip()
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "DELETE FROM session_events WHERE session_id = %s", (digest_channel(_OTHER),)
            )
        await record_session_event(
            digest_channel(_OTHER), CHECK_IN_KIND, {"requests": []}, dedupe_key="check-in-consumed"
        )
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "UPDATE session_events SET consumed_at = now() WHERE session_id = %s",
                (digest_channel(_OTHER),),
            )
        await supersede_unread_check_ins([_OTHER])
        async with db.connection(_dsn()) as conn:
            cursor = await conn.execute(
                "SELECT dedupe_key FROM session_events WHERE session_id = %s",
                (digest_channel(_OTHER),),
            )
            return [row[0] for row in await cursor.fetchall()]

    assert asyncio.run(_run()) == ["check-in-consumed"], (
        "the supersede destroyed a check-in somebody had already read"
    )


class _Concurrency:
    """Peak simultaneous mailbox writes, which is the whole of what D6 is about."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0

    @asynccontextmanager
    async def one(self) -> AsyncIterator[None]:
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            yield
        finally:
            self.live -= 1


def _staged_activities(
    pages: dict[str, CheckInPage], watch: _Concurrency, dwell: float
) -> list[Callable[..., Any]]:
    """The four activities, faked, so a test can control the population and time the loop."""

    @activity.defn(name="supersede_unread_check_ins")
    async def _supersede(owners: list[str]) -> int:
        return 0

    @activity.defn(name="collect_check_ins")
    async def _collect(after: str = "") -> CheckInPage:
        return pages[after]

    @activity.defn(name="record_session_event_activity")
    async def _notify(event: SessionEventInput) -> None:
        async with watch.one():
            await asyncio.sleep(dwell)

    @activity.defn(name="deliver_message_activity")
    async def _deliver(payload: OutboundMessage) -> list[str]:
        return []

    return [_supersede, _collect, _notify, _deliver]


def _page(owners: Sequence[str], *, after: str = "", more: bool = False) -> CheckInPage:
    """One synthetic page of one-question check-ins."""
    return CheckInPage(
        check_ins=[
            CheckIn(
                owner=owner,
                requests=[BlockedRequest(request_id=f"r-{owner}", kind="measurement", subject="s")],
            )
            for owner in owners
        ],
        after=after or (owners[-1] if owners else ""),
        more=more,
    )


def test_requesters_are_told_in_bounded_batches_rather_than_one_after_another() -> None:
    """Serial per requester × two activities × a 15-minute queue-wait bound is 30 minutes each.

    `light_write_queue_wait_timeout()` bounds the *wait* for a slot, not the work, so with
    `background-jobs` unserved the worst case was 30 minutes of serial wall clock per requester
    against a Schedule whose `run_timeout` is its own interval — past ~48 blocked requesters the run
    is still going when the next fires, and `ScheduleOverlapPolicy.SKIP` drops it silently. Batched,
    the waits overlap instead of summing.

    Asserted as observed concurrency rather than as elapsed time: a wall-clock threshold on a shared
    CI box is a flake, and the property is "more than one requester is in flight", bounded so the
    sweep cannot monopolise a queue that also carries hour-long searches' heartbeats.
    """
    owners = [f"check-in-batch-{index:02d}" for index in range(_CONCURRENT_REQUESTERS * 2 + 3)]
    watch = _Concurrency()

    async def _run() -> int:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with _sweep_worker(
                client, _staged_activities({"": _page(owners)}, watch, dwell=0.15)
            ):
                return await _sweep(client, "batched")

    delivered = asyncio.run(_run())

    assert delivered == len(owners), f"{delivered} of {len(owners)} requesters were told"
    assert watch.peak > 1, (
        "every requester was told one after another; the queue waits sum instead of overlapping"
    )
    assert watch.peak <= _CONCURRENT_REQUESTERS, (
        f"{watch.peak} mailbox writes were in flight at once against a bound of "
        f"{_CONCURRENT_REQUESTERS}: an unbounded fan-out starves the queue it shares"
    )


def test_the_delivery_count_is_not_re_counted_on_every_replay() -> None:
    """A workflow task replays its whole history on a cache miss, and this counter was unguarded.

    Measured before the fix: one real run of three deliveries followed by three replays moved
    `chemclaw_work_check_ins_total` 3 → 6 → 9 → 12. The dashboard reads
    `sum(increase(...[1d]))`, so the over-report is in the direction that makes a half-broken sweep
    look healthy — the panel's whole job is to say how many requesters were reached.
    `durable/notify.py` guards the identical pattern and `durable/orchestrator.py` writes out the
    rule.
    """
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Replayer

    owners = ["check-in-replay-a", "check-in-replay-b", "check-in-replay-c"]
    replays = 3

    async def _run() -> tuple[float, float, float]:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with _sweep_worker(
                client, _staged_activities({"": _page(owners)}, _Concurrency(), dwell=0.0)
            ):
                before = METRICS.value("chemclaw_work_check_ins_total")
                handle = await client.start_workflow(
                    CheckInWorkflow.run,
                    id="check-in-test-replay",
                    task_queue=settings.background_task_queue,
                )
                await handle.result()
                after_run = METRICS.value("chemclaw_work_check_ins_total")
                history = await handle.fetch_history()
            replayer = Replayer(
                workflows=[CheckInWorkflow],
                data_converter=pydantic_data_converter,
                workflow_runner=UnsandboxedWorkflowRunner(),
            )
            for _ in range(replays):
                await replayer.replay_workflow(history)
            return before, after_run, METRICS.value("chemclaw_work_check_ins_total")

    before, after_run, after_replays = asyncio.run(_run())

    assert after_run - before == len(owners), (
        f"the real run booked {after_run - before} of {len(owners)} deliveries"
    )
    assert after_replays == after_run, (
        f"{replays} replays of one run moved the counter to {after_replays} from {after_run}; the "
        "panel would report the replay multiple as delivered work"
    )


def test_a_run_that_spends_its_budget_defers_the_rest_instead_of_overrunning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of D6: batching makes the loop faster, it does not bound it.

    The Schedule's `run_timeout` is its own interval and its overlap policy is SKIP, so a run still
    going when the next fires does not queue — the next fire is *dropped*, silently, and the
    requesters it would have told hear nothing. A run that stops at half the interval and says what
    it deferred is the bounded version of the same outcome, and it leaves a line an operator can
    read instead of a Schedule that quietly fires half as often.
    """
    # 60 ms of budget against a 150 ms batch: the first batch overruns it by construction.
    monkeypatch.setattr(settings, "check_in_schedule_minutes", 0.002)
    first, second = ["check-in-budget-a", "check-in-budget-b"], ["check-in-budget-c"]
    pages = {
        "": _page(first, more=True),
        first[-1]: _page(second),
    }

    async def _run() -> int:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with _sweep_worker(client, _staged_activities(pages, _Concurrency(), dwell=0.15)):
                return await _sweep(client, "budget")

    delivered = asyncio.run(_run())

    assert delivered == len(first), (
        f"the sweep told {delivered} requesters; it should have stopped after the first page's "
        f"{len(first)} and deferred the rest to the next fire"
    )


def test_the_supersede_leaves_alone_a_requester_this_run_is_not_writing_to() -> None:
    """The narrowing that keeps a deferred requester's only notice, and its stated cost.

    A delete over every unread check-in in the table is simpler and is wrong in one direction that
    matters: `_run_budget` can stop a run part way, and a requester whose page never ran would have
    had last night's notice taken away with nothing put in its place. Scoped to the page about to be
    written, the worst case is a stale row for somebody who is no longer blocked — one row, gone the
    next time they are, and prunable the moment they read it.
    """
    from chemclaw.agent.session_events import record_session_event

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        async with db.connection(_dsn()) as conn:
            await conn.execute(
                "DELETE FROM session_events WHERE session_id = ANY(%s)",
                ([digest_channel(_OWNER), digest_channel(_OTHER)],),
            )
        for owner in (_OWNER, _OTHER):
            await record_session_event(
                digest_channel(owner),
                CHECK_IN_KIND,
                {"requests": []},
                dedupe_key=f"check-in-scope-{owner}",
            )
        await supersede_unread_check_ins([_OWNER])

        async def _rows(owner: str) -> list[str]:
            async with db.connection(_dsn()) as conn:
                cursor = await conn.execute(
                    "SELECT dedupe_key FROM session_events WHERE session_id = %s",
                    (digest_channel(owner),),
                )
                return [str(row[0]) for row in await cursor.fetchall()]

        return await _rows(_OWNER), await _rows(_OTHER)

    mine, theirs = asyncio.run(_run())

    assert mine == [], "the requester this run is about to write to keeps a stale notice"
    assert theirs == [f"check-in-scope-{_OTHER}"], (
        "a requester outside this page lost their only notice and got nothing in its place"
    )
