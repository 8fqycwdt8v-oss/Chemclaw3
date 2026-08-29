r"""The durable wait: a workflow that holds open a question for a person or an instrument.

**Nothing in this system could wait, and that was the finding rather than an omission.** A repo-wide
`grep -rn "workflow.signal\|wait_condition\|workflow.update" src/` returned zero hits: Temporal was
here and used exclusively for compute that starts and finishes. Every human decision was modelled as
refuse-and-retry — the plan gate refuses, the turn ends, a person clicks, a later turn proceeds —
which is right inside a conversation and cannot represent a process that outlives one. The
consequences ran in both directions: a project leader's entire working world is long-lived
multi-party process, and a *bench* chemist could not run a real screening campaign either, because
BO's value is proposing eight conditions and waiting a week for the plates.

One primitive, deliberately, with several callers rather than one shape per caller. A BO round
awaiting measurements, a gate awaiting a committee, a stability pull awaiting a timepoint and an
effect awaiting an approval are the same object: *a question, a deadline, an escalation, and an
answer that may never come.*

## The four properties that make it safe

**1. The answer is unsigned, so it is attribution and never authorization.**
`D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned` found the durable layer lifting a
role set out of an unsigned workflow payload and stamping it into the contextvar a privileged gate
reads. A signal is the same channel: anyone who can reach the broker can send one. So `Answer`
carries `answered_by` for the record and carries no roles, and **who may answer is decided at the
front door** (`api/routes/pending.py`) before the signal is ever sent. A workflow that trusted
`asked_of` would be a control that reads like one and is not.

**2. It cannot be answered twice, and an expiry racing a person does not decide by commit order.**
The workflow keeps the first answer it receives and ignores the rest; the store's `settle_request`
transitions only `WHERE state = 'waiting'` and reports whether it was the one that settled. Both
halves are needed: the workflow's own guard is per-run and the SQL guard holds across processes.

**3. A deadline is a property of the ask, not of the worker that runs it.** `due_at` is bound when
the wait opens, from the request. A reminder interval that fires N times before it is escalation;
reaching it is an outcome (`expired`), not a failure — a question nobody answered is a real answer
to a project leader, and raising here would retry the whole wait.

**4. It projects itself into a table, because Temporal cannot answer "what is waiting on me".**
The broker knows every open run; it does not know the subject line, the requester or the reason, and
listing per user means a visibility query against a self-hosted broker. `pending_requests` is that
projection, written by this workflow's own activities — exactly as `job_records` projects a finished
job. The workflow stays the authority on whether the wait is open.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.ids import stable_hash
    from chemclaw.durable import pending_store
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout
    from chemclaw.durable.registry import durable_activity, durable_workflow

#: The push-back kind a waiting request sends into the requester's mailbox. One kind for both the
#: opening notice and every reminder — the payload's `reminders` count is what distinguishes them,
#: so a surface renders one row that updates rather than a log that grows.
AWAITING_KIND = "awaiting-answer"

#: What a wait can be for. Bounded so an inbox can group without reading the subject line, and open
#: enough that a new caller does not need a migration: these are the four shapes that exist.
KINDS: tuple[str, ...] = ("measurement", "approval", "deliverable", "review")


class AwaitRequest(BaseModel):
    """The question a wait holds open."""

    kind: str = "approval"
    #: What is being asked, in the requester's words. Shown to the person being asked.
    subject: str
    #: Why it is being asked. The same discipline every durable launcher's `rationale` has: it is
    #: what the person answering reads, and what a reader months later finds.
    rationale: str = ""
    #: Advisory routing — an actor id or an entitlement, or '' for anyone entitled. Never a control.
    asked_of: str = ""
    requested_by: str = ""
    session_id: str = ""
    correlation_id: str = ""
    #: How long the question stays open. Clamped by `awaiting_max_days` at open time.
    deadline_days: float = 7.0
    #: How often to re-notify while it is open. 0 disables escalation.
    reminder_hours: float = 24.0


class Answer(BaseModel):
    """What came back. Attribution only — see property 1 above."""

    answered_by: str = ""
    #: Opaque to this workflow: a measurement set for a campaign, a decision for an approval.
    #: Typed and validated by whoever asked.
    payload: dict[str, Any] = Field(default_factory=dict)


class AwaitOutcome(BaseModel):
    """How the wait ended."""

    request_id: str
    #: `answered`, `expired` or `cancelled`. Never an exception: a question nobody answered is a
    #: result, and raising would retry the wait rather than report it.
    state: str
    answered_by: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    reminders: int = 0


def request_id_for(request: AwaitRequest) -> str:
    """The deterministic workflow id for this question, so asking twice is one wait.

    Keyed on what is being asked and of whom — not on the requester's session or correlation id,
    which change per turn. Two chemists asking the lab for the same measurement on the same subject
    should join one wait, exactly as two identical calculations share one cache row (D-011).
    """
    return "await-" + stable_hash(
        {"kind": request.kind, "subject": request.subject, "asked_of": request.asked_of}
    )


class _OpenInput(BaseModel):
    """The typed argument for `open_pending_request_activity`."""

    request_id: str
    request: AwaitRequest
    due_at: str
    #: The Temporal run this projection belongs to. It is what lets the store tell a *retry* of this
    #: activity (same run, update in place) from a *re-ask* after a lapsed deadline (new run, reopen
    #: the row) — two cases the projection used to collapse into one, leaving the re-asked question
    #: invisible and permanently unanswerable.
    run_id: str = ""


class _SettleInput(BaseModel):
    """The typed argument for `settle_pending_request_activity`."""

    request_id: str
    state: str
    answered_by: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


@durable_activity("background")
@activity.defn
async def open_pending_request_activity(payload: _OpenInput) -> None:
    """Project the open wait into `pending_requests`.

    Idempotent within a run and reopening across runs — `pending_store._OPEN` carries the argument.
    """
    await pending_store.open_request(
        request_id=payload.request_id,
        kind=payload.request.kind,
        subject=payload.request.subject,
        rationale=payload.request.rationale,
        asked_of=payload.request.asked_of,
        requested_by=payload.request.requested_by,
        session_id=payload.request.session_id,
        correlation_id=payload.request.correlation_id,
        due_at=datetime.fromisoformat(payload.due_at),
        run_id=payload.run_id,
    )


@durable_activity("background")
@activity.defn
async def settle_pending_request_activity(payload: _SettleInput) -> bool:
    """Settle the projection. Returns whether this call was the one that settled it."""
    return await pending_store.settle_request(
        payload.request_id,
        state=payload.state,
        answered_by=payload.answered_by,
        answer=payload.payload,
    )


@durable_activity("background")
@activity.defn
async def record_reminder_activity(request_id: str) -> None:
    """Count one escalation against a still-open request."""
    await pending_store.record_reminder(request_id)


@durable_workflow("background")
# Its failures must be able to *be* failures rather than parking in an unbounded workflow-task
# retry loop, for the reason `BoCampaignWorkflow` states: a parent waiting on a child that can
# never fail waits forever, and the person who asked is told "running" indefinitely.
@workflow.defn(failure_exception_types=[Exception])
class AwaitAnswerWorkflow:
    """Hold one question open until it is answered, its deadline passes, or it is cancelled."""

    def __init__(self) -> None:
        """Start with no answer and no escalations; both are workflow state, replayed with it."""
        self._answer: Answer | None = None
        self._reminders = 0

    @workflow.signal
    def provide(self, answer: dict[str, Any]) -> None:
        """Deliver the answer. The **first** one wins; later signals are ignored.

        Ignored rather than rejected: a signal has no reply channel, so raising here would fail the
        workflow task and retry the send forever. The front door refuses a second answer with a
        409 by reading the store, which is where a caller can actually be told.
        """
        if self._answer is None:
            self._answer = Answer.model_validate(answer)

    @workflow.query
    def waiting(self) -> bool:
        """Whether this wait is still open — the cheap check, with no database read."""
        return self._answer is None

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> AwaitOutcome:
        """Open the wait, escalate on a timer, and settle on the first of answer or deadline."""
        request = AwaitRequest.model_validate(payload)
        request_id = workflow.info().workflow_id
        # A `settings` read that is safe where the deadline's was not, and the difference is worth
        # stating: this feeds an activity *timeout*, which is an attribute of a command, while the
        # deadline fed the *number* of commands. Temporal's replay check compares the sequence and
        # type of commands, so a timeout that changed between runs is tolerated and a timer that
        # appears or vanishes is not.
        activity_timeout = timedelta(seconds=settings.awaiting_activity_timeout_seconds)

        # **The clamp is applied at the launch site, not here.** `due_at` decides how many timers
        # `_wait_until` schedules, so a `settings` read on this line put the *number of commands*
        # under a value that can change between an execution and its replay: lower
        # `CHEMCLAW_AWAITING_MAX_DAYS` while a 30-day wait is open, restart the worker, and the
        # replay computes a `due_at` already in the past, returns from the first iteration, and
        # emits a settle where history holds a timer — `NonDeterminismError`, retried forever, on
        # the workflow with the longest designed lifetime in the tree. `commitment_sync` states this
        # rule in the same package and routes its own config read through an activity for it.
        deadline = timedelta(days=max(0.0, request.deadline_days))
        due_at = workflow.now() + deadline
        await workflow.execute_activity(
            open_pending_request_activity,
            _OpenInput(
                request_id=request_id,
                request=request,
                due_at=due_at.isoformat(),
                run_id=workflow.info().run_id,
            ),
            start_to_close_timeout=activity_timeout,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        await self._notify(request, request_id, due_at.isoformat())

        try:
            await self._wait_until(due_at, request, request_id)
        except asyncio.CancelledError:
            # A cancelled wait must still stop *saying* it is open, or the inbox shows a question
            # nothing is listening for. The settle runs `ABANDON`, because an ordinary activity
            # scheduled from a cancelled workflow is cancelled with it and would never write.
            await self._settle(request_id, "cancelled", activity_timeout, detached=True)
            raise

        if self._answer is not None:
            await self._settle(
                request_id,
                "answered",
                activity_timeout,
                answered_by=self._answer.answered_by,
                payload=self._answer.payload,
            )
            return AwaitOutcome(
                request_id=request_id,
                state="answered",
                answered_by=self._answer.answered_by,
                payload=self._answer.payload,
                reminders=self._reminders,
            )

        await self._settle(request_id, "expired", activity_timeout)
        # Told, not silently abandoned: an unanswered question is exactly the thing a requester
        # needs to hear about, and it is the one outcome nobody is watching for.
        await self._push(
            request,
            {
                "request_id": request_id,
                "subject": request.subject,
                "state": "expired",
                "reminders": self._reminders,
            },
        )
        return AwaitOutcome(request_id=request_id, state="expired", reminders=self._reminders)

    async def _wait_until(self, due_at: datetime, request: AwaitRequest, request_id: str) -> None:
        """Block until answered or past `due_at`, re-notifying every `reminder_hours`.

        The reminder interval is a *timeout on the wait*, not a timer of its own: each expiry is one
        escalation and the loop resumes, so an answer arriving mid-interval is seen immediately
        rather than at the next tick.
        """
        interval = timedelta(hours=request.reminder_hours) if request.reminder_hours > 0 else None
        while self._answer is None:
            remaining = due_at - workflow.now()
            if remaining <= timedelta(0):
                return
            step = min(remaining, interval) if interval else remaining
            try:
                await workflow.wait_condition(lambda: self._answer is not None, timeout=step)
            except TimeoutError:
                # `wait_condition` *raises* on timeout rather than returning — so the escalation
                # tick and the deadline both arrive here as an exception, and letting it propagate
                # would fail the whole wait at the first chase. The loop's own `while` and the
                # `due_at` check below are what decide which of the two this was.
                pass
            if self._answer is None and workflow.now() < due_at:
                self._reminders += 1
                await workflow.execute_activity(
                    record_reminder_activity,
                    request_id,
                    start_to_close_timeout=timedelta(
                        seconds=settings.awaiting_activity_timeout_seconds
                    ),
                    schedule_to_start_timeout=queue_wait_timeout(),
                    retry_policy=BAD_DATA_RETRY,
                )
                await self._notify(request, request_id, due_at.isoformat())

    async def _notify(self, request: AwaitRequest, request_id: str, due_at: str) -> None:
        """Tell the requester's conversation that this is open, and how long it has left."""
        await self._push(
            request,
            {
                "request_id": request_id,
                "kind": request.kind,
                "subject": request.subject,
                "asked_of": request.asked_of,
                "due_at": due_at,
                "reminders": self._reminders,
                "state": "waiting",
            },
        )

    async def _push(self, request: AwaitRequest, payload: dict[str, Any]) -> None:
        """Push back into the requester's mailbox, when there is one.

        **A wait with no session is the ordinary case, not an edge one.** A campaign resumed by a
        Schedule, an effect approved out of an inbox, a question raised by a workflow rather than by
        a turn — none of them has a conversation to write to, and `SessionEventInput` requires a
        non-empty id. Building that input unconditionally raises `ValidationError` in *workflow*
        code, which `notify_session_best_effort` cannot catch (it guards the activity, not the
        argument), so the wait would fail outright on the path where nobody is listening — the exact
        inversion of best-effort. Measured: every sessionless wait failed before this guard.

        The request is still open, still in the inbox and still on its deadline; what is skipped is
        a notification with no addressee.
        """
        if not request.session_id:
            return
        await notify_session_best_effort(request.session_id, AWAITING_KIND, payload)

    async def _settle(
        self,
        request_id: str,
        state: str,
        timeout: timedelta,
        *,
        answered_by: str = "",
        payload: dict[str, Any] | None = None,
        detached: bool = False,
    ) -> None:
        """Move the projection to a terminal state, once."""
        await workflow.execute_activity(
            settle_pending_request_activity,
            _SettleInput(
                request_id=request_id,
                state=state,
                answered_by=answered_by,
                payload=payload or {},
            ),
            start_to_close_timeout=timeout,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
            cancellation_type=(
                workflow.ActivityCancellationType.ABANDON
                if detached
                else workflow.ActivityCancellationType.TRY_CANCEL
            ),
        )
