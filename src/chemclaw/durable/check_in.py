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
"""

import logging
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

logger = logging.getLogger(__name__)

#: The `session_events` kind a check-in lands under. A kind of its own rather than `DIGEST_KIND`,
#: because a surface must be able to tell "new knowledge matched your query" from "your own work is
#: still blocked" — they ask the reader for different things, and one mailbox carrying both under
#: one label would make the second unfindable among the first.
CHECK_IN_KIND = "work-check-in"

#: Every waiting request a requester is blocked on, oldest first.
#:
#: `requested_by <> ''` because a request opened with no actor cannot be reported *to* anyone — the
#: core rule refuses those at the tool, so a row without one predates that or came from a path that
#: is not the agent's. `due_at > now()` excludes the expired: those already reached their requester
#: through the wait's own expiry notice, and repeating it here would make this sweep a second,
#: worse copy of a notice that was already delivered.
_BLOCKED = """
    SELECT requested_by, request_id, kind, subject, rationale, asked_of,
           GREATEST(EXTRACT(EPOCH FROM now() - created_at) / 86400, 0)::int AS open_days,
           GREATEST(EXTRACT(EPOCH FROM due_at - now()) / 86400, 0)::int AS days_left
    FROM pending_requests
    WHERE state = 'waiting'
      AND requested_by <> ''
      AND due_at > now()
      AND created_at <= now() - %(quiet)s::interval
    ORDER BY requested_by, created_at
"""


class BlockedRequest(BaseModel):
    """One question a requester is waiting on, in the terms they asked it."""

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    #: Whole days the question has been open, and whole days until it expires. Rounded here rather
    #: than sent as timestamps because the recipient acts on "nine days, five left", and a surface
    #: that had to do the arithmetic would be a second place it could be done differently.
    open_days: int = 0
    days_left: int = 0


class CheckIn(BaseModel):
    """What one requester is blocked on."""

    owner: str
    requests: list[BlockedRequest] = Field(default_factory=list)


@durable_activity("background")
@activity.defn
async def collect_check_ins() -> list[CheckIn]:
    """Group every quiet, still-open request by the person who asked it.

    One query rather than one per requester: the population is bounded by how often people ask
    each other for things, which is human-paced (`durable/retention.py` makes the same argument
    for why `pending_requests` needs no sweep), so the whole set fits in one pass comfortably.
    """
    quiet = timedelta(days=settings.check_in_quiet_days)
    dsn = settings.session_store_dsn or settings.postgres_dsn
    async with db.connection(dsn) as conn:
        cursor = await conn.execute(_BLOCKED, {"quiet": quiet})
        rows = await cursor.fetchall()

    by_owner: dict[str, list[BlockedRequest]] = {}
    for requested_by, request_id, kind, subject, rationale, asked_of, open_days, days_left in rows:
        blocked = BlockedRequest(
            request_id=str(request_id),
            kind=str(kind),
            subject=str(subject),
            rationale=str(rationale or ""),
            asked_of=str(asked_of or ""),
            open_days=int(open_days),
            days_left=int(days_left),
        )
        by_owner.setdefault(str(requested_by), []).append(blocked)
    return [CheckIn(owner=owner, requests=items) for owner, items in by_owner.items()]


def _message(item: CheckIn) -> OutboundMessage:
    """The outbound copy, addressed to the requester and standing on its own.

    Carries the subject, the reason and the deadline for each — the three things needed to act —
    for the reason `awaiting._awaiting_message` gives: a channel reaches somebody with none of the
    context a surface has.
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
    return OutboundMessage(
        recipient=item.owner, subject="Work still waiting", body="\n".join(lines)
    )


@durable_workflow("background")
@workflow.defn
class CheckInWorkflow:
    """Tell each requester what of their own work is still blocked."""

    @workflow.run
    async def run(self) -> int:
        """Deliver every requester's check-in; return how many were sent."""
        check_ins = await workflow.execute_activity(
            collect_check_ins,
            start_to_close_timeout=timedelta(seconds=settings.check_in_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        delivered = 0
        for item in check_ins:
            # Best-effort per requester, the same reject-and-continue the digest uses: one broken
            # mailbox must not stop everybody else hearing that their work is stuck.
            sent = await notify_session_best_effort(
                digest_channel(item.owner),
                CHECK_IN_KIND,
                {"requests": [blocked.model_dump() for blocked in item.requests]},
            )
            if sent:
                delivered += 1
            # Strictly after the mailbox, and not part of whether this counted as delivered: the
            # mailbox is the durable handover a chemist sees when they open the app, and an
            # outbound channel is a courtesy on top of it. The digest learned that ordering the
            # hard way; this starts with it.
            await deliver_best_effort(_message(item))
        record_metric(lambda m: m.increment("chemclaw_work_check_ins_total", amount=delivered))
        return delivered
