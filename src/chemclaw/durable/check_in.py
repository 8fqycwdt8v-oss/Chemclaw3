"""Tell a requester their own work is still blocked — before the deadline, not after it.

`durable/awaiting.py` re-notifies on a timer already, and it notifies **`asked_of`**: the person who
has to do the thing. The requester — the chemist whose campaign is suspended on it — is written to
exactly once, on expiry, which `_awaiting_message` states outright ("while the wait is open it is an
ask, and the person who has to act is `asked_of`; when it expires it is a report, and the person who
needs to hear it is the requester"). That is the right split for those two notices and it leaves a
gap between them: `awaiting_max_days` is 90, so a requester can hear nothing about their own blocked
work for three months and then hear that it failed.

This is the sweep that closes it. Nothing here is about the *corpus* — `durable/digest.py` delivers
knowledge matching a standing query, and shares only this one's mailbox.

**It runs no model, and that is a decision rather than a simplification.** The obvious richer
version has an agent read the blocked work and say what it is blocking, and
`durable/template_activities.run_agent_step` is machinery that could do it — it takes a prompt, a
profile and a `write_tools` list that is a real narrowing carried across the activity boundary. What
it also takes is a `StepIdentity`, because a worker has no request context and an agent step is run
*as* somebody: that is what makes the audit trail name a real person and what makes
`enforce_tool_authz` decide against them. A template run has one because a person started it. A
Schedule has none. Synthesizing one from a `requested_by` string would be this system granting
itself a chemist's identity on a timer, for work that chemist did not ask for — which is not a
narrowing question that `write_tools=[]` answers, and is the shape
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` and `require_actor`'s reject-if-absent
rule exist to refuse.

So this reports and interprets nothing. What it sends is what the requester themselves wrote — their
own `subject` and `rationale` — plus how long it has been open and when it expires. That is
actionable without a model, and it keeps this sweep inside the line `planned_schedules()` draws:
jobs that make the corpus queryable, and none that decide what it means.

**Everything it carries is bounded, because the first version of it was not, and the population it
reads over is the one thing here that grows.** A sweep whose activity result grows with the corpus
of open asks fails at a size nobody notices until it is reached — measured against the live broker
at 10,000 waiting rows, `ServerError: Complete result exceeds size limit`, non-retryable, in 0.3 s,
with zero requesters told anything. That is the defect this feature exists to fix, reproduced by its
own scaling, and it arrives *permanently*: every subsequent night fails the same way. So the page
is bounded in rows (`_PAGE_ROWS`), each row's model-authored text is bounded in characters
(`_MAX_TEXT_CHARS`), the per-run delivery loop is bounded in wall clock (`_run_budget`), and the
mailbox is bounded to one unread row per requester (`supersede_unread_check_ins`). Each bound names
itself where it bites rather than truncating in silence, which is `kg/conflicts.py`'s rule and the
reason a reader can trust a short list.
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

#: The `session_events` kind a check-in lands under. A kind of its own rather than `DIGEST_KIND`,
#: because a surface must be able to tell "new knowledge matched your query" from "your own work is
#: still blocked" — they ask the reader for different things, and one mailbox carrying both under
#: one label would make the second unfindable among the first.
#:
#: It is also the `Message.kind` the outbound copy travels under, and for the *same* reason: the
#: file driver names a file `<kind>-<identity>`, so a check-in sent as `digest` lands in the
#: digest's own outbox file and folds into the webhook `Idempotency-Key` as a digest. The
#: distinction was honoured on the mailbox and broken on the channel, which is the half a chemist
#: who has closed the tab actually receives.
CHECK_IN_KIND = "work-check-in"

#: Rows one `collect_check_ins` page may carry, and the whole reason the activity takes a cursor.
#:
#: **Derived from Temporal's blob limit, not chosen.** An activity result over 2 MiB is refused by
#: the server with a non-retryable `Complete result exceeds size limit`, which fails the sweep
#: outright; with `_MAX_TEXT_CHARS` bounding the two free-text fields, one row costs at most
#: ~2.3 kB serialized, so 200 rows is ~460 kB — roughly a quarter of the limit, leaving room for a
#: future field without a second measurement. A module constant rather than a `Settings` field for
#: `pending_store._MAX_PAGE`'s reason: it is the bound that keeps one night's sweep inside the
#: protocol it runs on, not a deployment decision.
_PAGE_ROWS = 200

#: The longest `subject`/`rationale` a check-in carries per request.
#:
#: A row cap is not a byte cap while the text is unbounded, and it is: both fields are
#: model-authored, `pending_requests` declares no length on either, and one 4 MB rationale defeats
#: any number of rows. Truncation is *named* in the text the reader sees (`_abbreviated`) rather
#: than applied silently — a check-in that quietly dropped the reason the requester themselves wrote
#: would be worse than one that says how much it left out.
_MAX_TEXT_CHARS = 1_000

#: How many requesters one batch tells at once.
#:
#: The loop used to be serial, and each requester costs two activities whose *wait* is bounded at
#: `light_write_queue_wait_timeout()` (15 minutes) rather than at their work — so with
#: `background-jobs` unserved the worst case was 30 minutes of wall clock per requester, summed.
#: Batched, the waits overlap instead of summing. Bounded at eight rather than unbounded because
#: `background-jobs` is shared: its slots also carry the heartbeat timers for hour-long CREST
#: searches, and a sweep that fanned out over every requester at once would be the thing that
#: starves them.
_CONCURRENT_REQUESTERS = 8

#: The share of its own Schedule interval one run may spend before deferring the rest.
#:
#: The Schedule's `run_timeout` is `schedule_run_timeout_seconds` and its overlap policy is SKIP, so
#: a run still going when the next fires does not queue — the next fire is *dropped*, silently. Half
#: the interval is the bound that keeps a slow night from eating the following one, and the run ends
#: with a warning naming what it deferred instead of being terminated with nothing to read. What it
#: cannot fix is fairness: pages are ordered by `requested_by`, so the same tail is deferred every
#: night. That is accepted rather than sorted around, because the only condition under which this
#: bound bites is a `background-jobs` queue nothing is serving, and the fix for that is a worker —
#: the same conclusion `durable/notify.py` reaches about its own drops.
_RUN_BUDGET_FRACTION = 0.5

#: Every waiting request a requester is blocked on, oldest first, for one page of requesters.
#:
#: `requested_by <> ''` because a request opened with no actor cannot be reported *to* anyone — the
#: core rule refuses those at the tool, so a row without one predates that or came from a path that
#: is not the agent's. `due_at > now()` excludes the expired: those already reached their requester
#: through the wait's own expiry notice, and repeating it here would make this sweep a second,
#: worse copy of a notice that was already delivered.
#:
#: `requested_by > %(after)s` is the keyset continuation. On page one it says exactly what the
#: predicate above it says; on every later page it is what makes the walk forward rather than
#: re-reading the same rows, and it is a *requester* cursor rather than a row cursor so that one
#: person's questions are never split across two check-ins — two mailbox rows for one requester
#: would be two notices that each claim to be the whole of their blocked work.
#:
#: `FLOOR` rather than `::int`, on both day counts. `::int` **rounds**, so 4.6 days left arrived as
#: "5 left" — overstating a deadline by up to half a day, in the direction that makes a requester
#: act later than they can afford to. Flooring is the conservative direction for a deadline, and
#: `GREATEST(..., 0)` already keeps it off the negative side.
#:
#: `session_id` is selected for the same reason `kind` always was: it is a column of the row this
#: query already reads, and the surface at the other end ends every other inbox row in "open the
#: conversation". It is the requester's *own* session — this whole query is scoped to
#: `requested_by` — so it reaches nobody it does not already belong to.
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
#: **Scoped three ways, and each scope is load-bearing.** `kind` keeps the delete off another
#: consumer's rows — the same discipline that makes the destructive claim in
#: `api/routes/streams.read_check_ins` safe. `consumed_at IS NULL` keeps it off what a chemist has
#: already claimed, which is the only record that anything was ever delivered. And `session_id`
#: keeps it to the requesters this run is *about to write to*: a delete over every check-in in the
#: table would take the previous night's notice from a requester the run then defers
#: (`_run_budget`) and replace it with nothing, which is a worse outcome than the duplicate it was
#: removing.
#:
#: The cost of that narrowing is stated rather than discovered: a requester whose question has since
#: been answered is in no page, so their last unread check-in stays in the mailbox saying "still
#: waiting". It is one row, it is superseded the next time they are blocked, and reading it consumes
#: it — which is what makes it prunable. The population stays bounded at one per requester either
#: way, which is the whole of what this fixes.
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
    #: The conversation the question was asked in, or `""` — a wait opened by a BO plate run or a
    #: connector job has none, and `AwaitRequest.session_id` defaults to empty for exactly those.
    #: Defaulted here for `CheckIn.truncated`'s reason: a run opened on the previous release
    #: replays a recorded result that has no such key.
    session_id: str = ""
    #: Whole days the question has been open, and whole days until it expires. Rounded here rather
    #: than sent as timestamps because the recipient acts on "nine days, five left", and a surface
    #: that had to do the arithmetic would be a second place it could be done differently.
    open_days: int = 0
    days_left: int = 0


class CheckIn(BaseModel):
    """What one requester is blocked on."""

    owner: str
    requests: list[BlockedRequest] = Field(default_factory=list)
    #: Whether this requester has more waiting questions than one page carries.
    #:
    #: Defaulted for the reason every field added to a durable payload must be (`DigestItem` makes
    #: the same argument): a run opened on the previous release replays a recorded result that has
    #: no such key.
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

    Named rather than silent, because a truncation a reader cannot see reads as completeness: a
    rationale cut at a sentence boundary looks like the whole reason the requester wrote, and acting
    on half a reason is worse than knowing there is more to read.
    """
    dropped = full_chars - len(text)
    if dropped <= 0:
        return text
    return f"{text}… [{dropped} more character(s) not carried; open the request to read it]"


@durable_activity("background")
@activity.defn
async def supersede_unread_check_ins(owners: list[str]) -> int:
    """Delete these requesters' unread check-ins, so tonight's is the only one left to read.

    **Without this the sweep re-sends nightly for ever into a mailbox retention can never empty.**
    There was no watermark of any kind, so a request open the full `awaiting_max_days` produced ~87
    consecutive nightly rows per requester, each differing from the last by one integer — the exact
    habituation `check_in_quiet_days` exists to prevent ("a check-in that said so on the first night
    would train its reader to ignore the second"), reintroduced one layer down. And they are
    immortal: `retention._PRUNABLE["session_events"]` prunes only `consumed_at IS NOT NULL`, so
    until a surface calls `GET /check-ins` every row this writes is unread for ever (measured at
    ~475 kB of JSONB per owner over one 90-day wait).

    A *supersede* rather than a watermark, because the two say different things and only one of them
    is true here. A watermark claims "you have been told about these"; this notice claims "this is
    what is blocked **now**", and last night's copy is not history, it is a stale answer to the same
    question. Replacing it bounds the population at one row per requester without the sweep having
    to remember anything between runs.

    Once per *batch*, immediately before that batch's requesters are written to, rather than once
    per run, per page or per requester. Once per run would have to be a delete over the whole
    table, and once per page was the same defect one level down: the run budget can defer partway
    through a page, so either took a deferred requester's notice away and put nothing in its place
    (see `_SUPERSEDE`). Once per requester would put a third light write on the queue
    `_CONCURRENT_REQUESTERS` exists to keep room on; a batch is the unit already delivered whole.

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

    **This was one unbounded query, and the belief that made it one is worth keeping visible.** Its
    docstring argued that "the population is bounded by how often people ask each other for things,
    which is human-paced ... so the whole set fits in one pass comfortably" — true of the *rate* and
    false of the *stock*, because the population is 90 days of open asks and nothing here was
    proportional to the rate. Measured end to end on the live broker at 10,000 waiting rows, the
    activity's result was refused: `ServerError: Complete result exceeds size limit`, non-retryable,
    the whole sweep dead in 0.3 s, and dead again every night thereafter. With ~1 kB of
    model-authored text per request the crossover is around 2,000 rows.

    So the page is bounded three ways and each is visible to the reader: `_PAGE_ROWS` rows,
    `_MAX_TEXT_CHARS` of each free-text field with `_abbreviated` naming what it cut, and — the one
    that needs the +1 probe below — a *requester* boundary, so that a page never carries half of
    somebody's blocked work.

    The `LIMIT` asks for one row more than a page. That extra row is not delivered; it is how this
    tells "the page is full" from "the table is exhausted" without a second count query, and it is
    what makes the last requester in a full page *suspect* — there may be more of theirs beyond the
    cut — so that requester is dropped and re-read whole on the next page. The single case where
    that cannot be done is one requester whose own questions fill a page alone: they are carried
    truncated and `CheckIn.truncated` says so, because dropping them would make the walk stand
    still.

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
    # The last requester in a full page may have more rows past the cut. Drop them and start the
    # next page at them — unless they are the only requester here, in which case dropping them would
    # leave the cursor where it was and the walk would never advance.
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

    Carries the subject, the reason and the deadline for each — the three things needed to act —
    for the reason `awaiting._awaiting_message` gives: a channel reaches somebody with none of the
    context a surface has.

    **`kind` is `CHECK_IN_KIND` and was the default.** Omitting it took `OutboundMessage`'s
    `"digest"`, so every check-in was written into the standing-query digest's own outbox file
    (`FileDeliveryDriver` names files `<kind>-<identity>`) and folded into the webhook
    `Idempotency-Key` as a digest — the central claim that a surface can tell "new knowledge matched
    your query" from "your own work is still blocked", honoured on the mailbox and broken on the
    channel.
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

    Derived from the Schedule's own interval rather than configured beside it, so the two cannot
    drift into a run that outlives the fire after it — see `_RUN_BUDGET_FRACTION` for why that is
    silent when it happens.
    """
    return timedelta(minutes=settings.check_in_schedule_minutes * _RUN_BUDGET_FRACTION)


@durable_workflow("background")
# **Fails rather than parks**, and for this sweep that is not the usual periodic-job
# argument (`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`).
# Its neighbours on the nightly queue park because nobody is waiting on them and a bug
# should wait for a fix. Here silence *is* the output: a check-in that delivers nothing is
# indistinguishable from "nothing of yours is blocked", which is the good state — so a
# parked run reads as reassurance. Under `ScheduleOverlapPolicy.SKIP` one parked run then
# skips every subsequent night, and a requester goes back to hearing nothing until expiry,
# which is precisely the defect this whole sweep exists to fix. A failure reaches an
# operator through `ScheduleHealth.last_outcome`; a park reaches nobody.
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
            # **Gated, because it moves a command.** The shipped code superseded once per page,
            # before the first batch; per batch schedules a supersede where a recorded history of
            # a page longer than `_CONCURRENT_REQUESTERS` holds the second batch's deliveries, and
            # this workflow fails rather than parks, so the unguarded change turned a redeploy
            # mid-sweep into that night's failed run. Asked only for a non-empty page, the one
            # shape the two versions differ on. The id may never be reused.
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
                # Immediately before this batch is written and scoped to it: a check-in is a
                # statement about *now*, so last night's unread copy is a stale answer to the same
                # question rather than history. See `supersede_unread_check_ins`. Per batch rather
                # than per page because the budget check below can defer mid-page, and a page-wide
                # delete took every later requester's notice and replaced it with nothing. An empty
                # page has no batch, so a quiet night still costs no activity at all.
                if per_batch:
                    dropped += await workflow.execute_activity(
                        supersede_unread_check_ins,
                        [item.owner for item in batch],
                        start_to_close_timeout=timeout,
                        schedule_to_start_timeout=queue_wait_timeout(),
                        retry_policy=BAD_DATA_RETRY,
                    )
                # Concurrently across requesters, serially within one — see `_tell`. Best-effort per
                # requester, the same reject-and-continue the digest uses: one broken mailbox must
                # not stop everybody else hearing that their work is stuck.
                delivered += sum(await asyncio.gather(*(self._tell(one) for one in batch)))
                remaining = len(page.check_ins) - start - len(batch)
                if workflow.now() >= deadline and (remaining or page.more):
                    deferred = True
                    break
            if not page.more:
                break
            if page.after <= after:
                # Cannot happen while `collect_check_ins` always keeps at least one requester, and
                # asserted here anyway: a workflow that re-reads one page for ever is a worse
                # failure than one that stops short and says so.
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
        # Guarded, because a workflow task replays its whole history on every cache miss and an
        # unguarded increment counts one real delivery once per replay: measured, one run of three
        # deliveries followed by three replays moved this counter 3 → 6 → 9 → 12. The dashboard
        # panel reads `increase(...[1d])`, so the over-report is in the direction that makes a
        # half-broken sweep look healthy. `durable/notify.py` guards the identical pattern and
        # `durable/orchestrator.py` writes out the rule.
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_work_check_ins_total", amount=delivered))
            # Counted rather than only logged, for the reason the declaration gives: a run that
            # stops short is under-delivering the notice this sweep exists to send, and it is
            # indistinguishable from a quiet night on `chemclaw_work_check_ins_total` alone.
            if deferred:
                record_metric(lambda m: m.increment("chemclaw_work_check_in_deferrals_total"))
        return delivered

    async def _tell(self, item: CheckIn) -> bool:
        """One requester's mailbox row and then their outbound copy; True if the mailbox took it.

        The two stay **serial within a requester**: the mailbox is the durable handover a chemist
        sees when they open the app, and an outbound channel is a courtesy on top of it. The digest
        learned that ordering the hard way; this starts with it. What overlaps is one requester
        against another, which is where the wall clock was being spent.

        **`truncated` travels on the mailbox row and used to travel only in the email.** The
        payload was `{"requests": [...]}` and nothing else, so `_message` told a requester with
        more than `_PAGE_ROWS` open questions that their list was short and the mailbox the app
        actually opens said nothing — a list that looks complete, which is the one thing
        `_abbreviated` and `kg/conflicts.py` both refuse to do to a reader. Beside `requests`
        rather than inside each one, because it is a property of the page: what the route does with
        it is the route's business.
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
