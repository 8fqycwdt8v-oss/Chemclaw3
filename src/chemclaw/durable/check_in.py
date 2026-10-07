"""Tell a requester their own work is still blocked — before the deadline, not after it.

`durable/awaiting.py` re-notifies `asked_of` (the person who has to act) and tells the requester
only on expiry, which may be `awaiting_max_days` later. This sweep closes that gap.

It runs no model: an agent step needs a `StepIdentity`, and a Schedule has none to give, so
synthesizing one would be the system acting as a chemist on a timer. It sends what the
requester wrote (`subject`, `rationale`) plus how long it has been open and when it expires.

Everything is bounded, because the population of open asks grows: rows per page
(`_PAGE_ROWS`), characters per free-text field (`_MAX_TEXT_CHARS`), wall clock per run
(`_run_budget`), and one unread mailbox row per requester (`supersede_unread_check_ins`). Each
bound is named where it bites rather than truncating silently.
"""

import asyncio
from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core import db
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.deliver_message import OutboundMessage, deliver_best_effort
    from chemclaw.durable.digest import digest_channel
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout
    from chemclaw.durable.registry import durable_activity, durable_workflow

#: The `session_events` kind and `Message.kind` a check-in travels under, distinct from
#: `DIGEST_KIND` so a reader can tell "new knowledge matched your query" from "your own work is
#: still blocked" on the mailbox and on every channel.
CHECK_IN_KIND = "work-check-in"

#: Rows one `collect_check_ins` page may carry. Derived from Temporal's 2 MiB activity-result
#: limit: with `_MAX_TEXT_CHARS`, one row is at most ~2.3 kB, so a page stays near a quarter of it.
_PAGE_ROWS = 200

#: The longest `subject`/`rationale` a check-in carries per request. Both are model-authored and
#: unbounded in the table; truncation is named in the text (`_abbreviated`).
_MAX_TEXT_CHARS = 1_000

#: How many requesters one batch tells at once, so queue waits overlap instead of summing. Bounded
#: because `background-jobs` is shared with heartbeat timers for long-running jobs.
_CONCURRENT_REQUESTERS = 8

#: The share of its Schedule interval one run may spend before deferring the rest. Under SKIP, a
#: run still going when the next fires drops that fire silently; ending early with a warning is
#: better. Pages are ordered by `requested_by`, so the same tail is deferred each night — accepted,
#: since this only bites when nothing serves the queue.
_RUN_BUDGET_FRACTION = 0.5

#: Every waiting request a requester is blocked on, oldest first, for one page of requesters.
#:
#: `requested_by <> ''`: a request with no actor cannot be reported to anyone. `due_at > now()`:
#: expired requests already reached their requester through the expiry notice. The keyset cursor
#: is on requester, not row, so one person's questions are never split across two check-ins.
#: `FLOOR` rather than `::int` so a deadline is never overstated. `session_id` is the requester's
#: own session.
_BLOCKED = """
    SELECT requested_by, request_id, kind, session_id,
           left(subject, %(chars)s) AS subject,
           length(subject) AS subject_chars,
           left(coalesce(rationale, ''), %(chars)s) AS rationale,
           length(coalesce(rationale, '')) AS rationale_chars,
           coalesce(asked_of, '') AS asked_of,
           FLOOR(GREATEST(EXTRACT(EPOCH FROM now() - created_at) / 86400, 0))::int AS open_days,
           FLOOR(GREATEST(EXTRACT(EPOCH FROM due_at - now()) / 86400, 0))::int AS days_left
    FROM pending_requests
    WHERE state = 'waiting'
      AND requested_by <> ''
      AND requested_by > %(after)s
      AND due_at > now()
      AND created_at <= now() - %(quiet)s::interval
    ORDER BY requested_by, created_at
    LIMIT %(limit)s
"""

#: Drop the check-ins these requesters have not read, so tonight's is the only one in the mailbox.
#:
#: Scoped by `kind` (another consumer's rows), `consumed_at IS NULL` (what was already claimed),
#: and `session_id` (only requesters this run is about to write to, so a deferred requester keeps
#: last night's notice). A requester whose question was since answered keeps one stale unread row
#: until it is read or superseded.
_SUPERSEDE = """
    DELETE FROM session_events
    WHERE kind = %(kind)s AND consumed_at IS NULL AND session_id = ANY(%(channels)s)
"""


class BlockedRequest(BaseModel):
    """One question a requester is waiting on, in the terms they asked it."""

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    #: The conversation the question was asked in, or `""` (BO plate runs and connector jobs have
    #: none). Defaulted so an older recorded result still decodes on replay.
    session_id: str = ""
    #: Whole days open and whole days until expiry, rounded here so no surface computes them
    #: differently.
    open_days: int = 0
    days_left: int = 0


class CheckIn(BaseModel):
    """What one requester is blocked on."""

    owner: str
    requests: list[BlockedRequest] = Field(default_factory=list)
    #: Whether this requester has more waiting questions than one page carries. Defaulted so an
    #: older recorded result still decodes on replay.
    truncated: bool = False


class CheckInPage(BaseModel):
    """One bounded page of check-ins, and where the next one starts.

    A page rather than the whole set, because the whole set is an activity *result* and Temporal
    refuses one over 2 MiB — see `_PAGE_ROWS`. The cursor is the last requester this page settled in
    full, so the next page asks for `requested_by > after` and no requester is ever split.
    """

    check_ins: list[CheckIn] = Field(default_factory=list)
    #: The last requester this page carries in full; the next page starts strictly after it.
    after: str = ""
    #: Whether rows remain beyond this page. The workflow loops on exactly this.
    more: bool = False


def _abbreviated(text: str, full_chars: int) -> str:
    """`text` as the query truncated it, saying how much it left out — or unchanged.

    Named rather than silent, because a truncation a reader cannot see reads as completeness.
    """
    dropped = full_chars - len(text)
    if dropped <= 0:
        return text
    return f"{text}… [{dropped} more character(s) not carried; open the request to read it]"


@durable_activity("background")
@activity.defn
async def supersede_unread_check_ins(owners: list[str]) -> int:
    """Delete these requesters' unread check-ins, so tonight's is the only one left to read.

    A check-in states what is blocked *now*, so last night's unread copy is stale rather than
    history; superseding bounds the mailbox at one row per requester with no state between runs.
    Run once per batch, immediately before that batch is written, so a requester deferred by the
    run budget keeps their previous notice.

    Args:
        owners: the requesters whose stale notices this drops — the batch about to be delivered.

    Returns:
        How many stale notices were dropped, for the run's own log line.
    """
    if not owners:
        return 0
    channels = [digest_channel(owner) for owner in owners]
    dsn = settings.session_store_dsn or settings.postgres_dsn
    async with db.connection(dsn, operation="check_in_supersede") as conn:
        cursor = await conn.execute(_SUPERSEDE, {"kind": CHECK_IN_KIND, "channels": channels})
        return int(cursor.rowcount)


@durable_activity("background")
@activity.defn
async def collect_check_ins(after: str = "") -> CheckInPage:
    """One bounded page of quiet, still-open requests, grouped by the person who asked them.

    Bounded by `_PAGE_ROWS`, by `_MAX_TEXT_CHARS` per free-text field, and at a requester boundary
    so a page never carries half of somebody's work. The query fetches one extra row to tell "page
    full" from "table exhausted"; in a full page the last requester is dropped and re-read whole on
    the next page — unless they alone fill it, in which case they are carried with
    `CheckIn.truncated` set so the walk still advances.

    Args:
        after: the `requested_by` the previous page settled in full; `""` starts at the beginning.

    Returns:
        The page, the cursor for the next one, and whether there is a next one.
    """
    quiet = timedelta(days=settings.check_in_quiet_days)
    dsn = settings.session_store_dsn or settings.postgres_dsn
    async with db.connection(dsn, operation="check_in_collect") as conn:
        cursor = await conn.execute(
            _BLOCKED,
            {
                "quiet": quiet,
                "after": after,
                "chars": _MAX_TEXT_CHARS,
                # One more than a page: the probe that says whether a next page exists.
                "limit": _PAGE_ROWS + 1,
            },
        )
        rows = await cursor.fetchall()

    more = len(rows) > _PAGE_ROWS
    grouped: dict[str, list[BlockedRequest]] = {}
    for (
        requested_by,
        request_id,
        kind,
        session_id,
        subject,
        subject_chars,
        rationale,
        rationale_chars,
        asked_of,
        open_days,
        days_left,
    ) in rows[:_PAGE_ROWS]:
        grouped.setdefault(str(requested_by), []).append(
            BlockedRequest(
                request_id=str(request_id),
                kind=str(kind),
                session_id=str(session_id),
                subject=_abbreviated(str(subject), int(subject_chars)),
                rationale=_abbreviated(str(rationale), int(rationale_chars)),
                asked_of=str(asked_of),
                open_days=int(open_days),
                days_left=int(days_left),
            )
        )

    owners = list(grouped)
    if not owners:
        return CheckInPage(check_ins=[], after=after, more=False)
    # The last requester in a full page may have more rows past the cut: drop them and start the
    # next page at them, unless they are the only requester here (the walk must advance).
    truncated_owner = ""
    if more and len(owners) > 1:
        del grouped[owners[-1]]
        owners.pop()
    elif more:
        truncated_owner = owners[-1]
    return CheckInPage(
        check_ins=[
            CheckIn(owner=owner, requests=items, truncated=owner == truncated_owner)
            for owner, items in grouped.items()
        ],
        after=owners[-1],
        more=more,
    )


def _message(item: CheckIn) -> OutboundMessage:
    """The outbound copy, addressed to the requester and standing on its own.

    Carries the subject, reason and deadline for each request, since a channel reader lacks the
    app's context. `kind` is `CHECK_IN_KIND` so it is not filed or keyed as a digest.
    """
    lines = [
        f"You have {len(item.requests)} question(s) still waiting on somebody, and none of them "
        "has been answered yet."
    ]
    for blocked in item.requests:
        asked_of = blocked.asked_of or "anyone entitled"
        lines.append(
            f"- {blocked.subject} (asked of {asked_of}; open {blocked.open_days} day(s), "
            f"{blocked.days_left} left) [{blocked.request_id}]"
        )
        if blocked.rationale:
            lines.append(f"  because: {blocked.rationale}")
    if item.truncated:
        lines.append(
            f"Only the {len(item.requests)} oldest are listed — you have more waiting than one "
            "check-in carries. Open your requests to see the rest."
        )
    return OutboundMessage(
        recipient=item.owner,
        subject="Work still waiting",
        body="\n".join(lines),
        kind=CHECK_IN_KIND,
    )


def _run_budget() -> timedelta:
    """How long one sweep may spend delivering before deferring the rest to the next fire.

    Derived from the Schedule's own interval so the two cannot drift apart.
    """
    return timedelta(minutes=settings.check_in_schedule_minutes * _RUN_BUDGET_FRACTION)


@durable_workflow("background")
# Fails rather than parks: silence is this sweep's good state, so a parked run reads as
# reassurance, and under SKIP it would suppress every later night. A failure reaches an operator
# through `ScheduleHealth.last_outcome`.
@workflow.defn(failure_exception_types=[Exception])
class CheckInWorkflow:
    """Tell each requester what of their own work is still blocked."""

    @workflow.run
    async def run(self) -> int:
        """Deliver every requester's check-in; return how many were sent."""
        timeout = timedelta(seconds=settings.check_in_timeout_seconds)
        deadline = workflow.now() + _run_budget()
        delivered = 0
        dropped = 0
        after = ""
        deferred = False
        while not deferred:
            page = await workflow.execute_activity(
                collect_check_ins,
                after,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            # Patched because superseding per batch moves a command relative to the old per-page
            # supersede, which would fail a mid-sweep replay. Asked only for a non-empty page, the
            # one shape the versions differ on. The patch id may never be reused.
            per_batch = bool(page.check_ins) and workflow.patched("check-in-supersede-per-batch")
            if page.check_ins and not per_batch:
                dropped += await workflow.execute_activity(
                    supersede_unread_check_ins,
                    [item.owner for item in page.check_ins],
                    start_to_close_timeout=timeout,
                    schedule_to_start_timeout=queue_wait_timeout(),
                    retry_policy=BAD_DATA_RETRY,
                )
            for start in range(0, len(page.check_ins), _CONCURRENT_REQUESTERS):
                batch = page.check_ins[start : start + _CONCURRENT_REQUESTERS]
                # Immediately before this batch is written and scoped to it, so a deferral mid-page
                # leaves later requesters' notices intact. An empty page costs no activity.
                if per_batch:
                    dropped += await workflow.execute_activity(
                        supersede_unread_check_ins,
                        [item.owner for item in batch],
                        start_to_close_timeout=timeout,
                        schedule_to_start_timeout=queue_wait_timeout(),
                        retry_policy=BAD_DATA_RETRY,
                    )
                # Concurrent across requesters, serial within one (see `_tell`); best-effort per
                # requester.
                delivered += sum(await asyncio.gather(*(self._tell(one) for one in batch)))
                remaining = len(page.check_ins) - start - len(batch)
                if workflow.now() >= deadline and (remaining or page.more):
                    deferred = True
                    break
            if not page.more:
                break
            if page.after <= after:
                # Unreachable while `collect_check_ins` keeps at least one requester; guarded so the
                # walk can never loop on one page.
                workflow.logger.error(
                    "the check-in page cursor did not advance past %r; stopping rather than "
                    "re-reading the same page for ever",
                    after,
                )
                break
            after = page.after
        workflow.logger.info("the check-in sweep superseded %d unread notice(s)", dropped)
        if deferred:
            workflow.logger.warning(
                "the check-in sweep spent its run budget of %s after telling %d requester(s); the "
                "rest are deferred to the next fire. A run that overran would be dropped whole by "
                "ScheduleOverlapPolicy.SKIP, so this is the bounded version of the same outcome — "
                "if it repeats, the background queue is not being served fast enough",
                _run_budget(),
                delivered,
            )
        # Guarded: a replay re-runs this code, and an unguarded increment would count each delivery
        # once per replay.
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_work_check_ins_total", amount=delivered))
            # Counted so a run that stopped short is distinguishable from a quiet night.
            if deferred:
                record_metric(lambda m: m.increment("chemclaw_work_check_in_deferrals_total"))
        return delivered

    async def _tell(self, item: CheckIn) -> bool:
        """One requester's mailbox row and then their outbound copy; True if the mailbox took it.

        Serial within a requester: the mailbox is the durable handover and the channel a courtesy on
        top. `truncated` travels on the mailbox row beside `requests`, so the app can say the list
        is incomplete.
        """
        sent = await notify_session_best_effort(
            digest_channel(item.owner),
            CHECK_IN_KIND,
            {
                "requests": [blocked.model_dump() for blocked in item.requests],
                "truncated": item.truncated,
            },
        )
        await deliver_best_effort(_message(item))
        return sent
