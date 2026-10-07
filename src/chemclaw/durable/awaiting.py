r"""The durable wait: a workflow that holds open a question for a person or an instrument.

One primitive with several callers: a BO round awaiting measurements, a gate awaiting a
committee and an effect awaiting approval are all a question, a deadline, an escalation, and an
answer that may never come.

1. **The answer is attribution, never authorization.** A signal is unsigned, so `Answer`
   carries `answered_by` and no roles; who may answer is decided at the front door
   (`api/routes/pending.py`) before the signal is sent.
2. **It cannot be answered twice.** The workflow keeps the first answer; the store's
   `settle_request` transitions only `WHERE state = 'waiting'`, which holds across processes.
3. **A deadline belongs to the ask.** `due_at` is bound when the wait opens; reminders fire
   before it, and reaching it is an outcome (`expired`), not a failure.
4. **It projects itself into `pending_requests`**, because Temporal cannot answer "what is
   waiting on me". The workflow stays the authority on whether the wait is open.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator
from temporalio import activity, workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.exceptions import CancelledError as TemporalCancelledError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.ids import stable_hash
    from chemclaw.core.temporal_client import connect
    from chemclaw.durable import pending_store
    from chemclaw.durable.deliver_message import OutboundMessage, deliver_best_effort
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.kg.note import cited_ids, is_note_slug

# The push-back kind sent into the requester's mailbox, for the opening notice and every reminder;
# the payload's `reminders` count distinguishes them, so a surface renders one updating row.
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
    # The knowledge notes this question rests on, derived from the `[[wikilinks]]` in `subject` and
    # `rationale`. The front door refuses an answer once any of them is superseded or refuted.
    premise_note_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_premise(self) -> "AwaitRequest":
        r"""Derive the premise from this question's own citations, on every producer.

        Derived here so no producer can omit it. Pure and deterministic, so safe on replay. Filtered
        through `is_note_slug`, which also defangs: the ids are cut from caller-supplied free text
        and
        are later shown to the model raw.
        """
        cited = cited_ids(f"{self.subject}\n{self.rationale}")
        self.premise_note_ids = [note_id for note_id in cited if is_note_slug(note_id)]
        return self

    #: How long the question stays open. Clamped against `awaiting_max_days` by
    #: `open_pending_request_activity` — one place, so no caller can pass an unbounded value.
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

    Keyed on what is asked and of whom, not on session or correlation id, so two chemists asking
    the same thing join one wait.
    """
    return "await-" + stable_hash(
        {"kind": request.kind, "subject": request.subject, "asked_of": request.asked_of}
    )


async def open_wait(request: AwaitRequest) -> tuple[str, bool]:
    """Open the wait this question describes, or join the one already open for it.

    The shared launch idiom: a deterministic id, `ALLOW_DUPLICATE`, and the already-started catch.
    `ALLOW_DUPLICATE` because an expired wait completes normally, and any stricter policy would make
    a lapsed question unaskable forever; a re-ask while the first is open joins it via
    `WorkflowAlreadyStartedError`.

    Args:
        request: The question to hold open. Its `subject`, `kind` and `asked_of` decide what joins
            what, through `request_id_for`.

    Returns:
        The wait's id, and whether this call opened it. A caller announces a launch only on `True`.

    Raises:
        Whatever the broker raises; callers handle a failure differently, so it is not swallowed.
    """
    request_id = request_id_for(request)
    client = await connect()
    try:
        await client.start_workflow(
            AwaitAnswerWorkflow.run,
            request.model_dump(mode="json"),
            id=request_id,
            task_queue=settings.background_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return request_id, False
    return request_id, True


def _awaiting_message(request: AwaitRequest, payload: dict[str, Any]) -> OutboundMessage:
    """The outbound copy of one wait notice — who it goes to, and what it says.

    While the wait is open the notice is an ask, addressed to `asked_of`; on expiry it is a report,
    addressed to the requester. The copy stands alone (reason, deadline, request id), since its
    reader has no surface that knows what a pending request is.

    Runs in workflow code, so every payload key is read through a default or a guard: a `KeyError`
    here could not be caught.
    """
    request_id = str(payload.get("request_id", ""))
    reminders = int(payload.get("reminders", 0) or 0)
    if payload.get("state") == "expired":
        lines = [f"Nobody answered in time, after {reminders} reminder(s)."]
        if request.rationale:
            lines.append(request.rationale)
        lines.append(f"It was asked of {request.asked_of or 'anyone entitled'}.")
        lines.append(f"The request was {request_id}.")
        return OutboundMessage(
            recipient=request.requested_by,
            subject=f"No answer: {request.subject}",
            body="\n".join(lines),
            kind="awaiting",
            correlation_id=request.correlation_id,
        )
    lines = [request.rationale] if request.rationale else []
    if payload.get("due_at"):
        lines.append(f"Due {payload['due_at']}.")
    if reminders:
        lines.append(f"Reminder {reminders} — this has been open since it was asked.")
    lines.append(f"Answer it against request {request_id}.")
    if request.asked_of:
        return OutboundMessage(
            recipient=request.asked_of,
            subject=f"Waiting on you: {request.subject}",
            body="\n".join(lines),
            kind="awaiting",
            correlation_id=request.correlation_id,
        )
    # Nobody was named (`asked_of` empty), so the requester is told the question is open rather than
    # nobody being told; the subject says no owner was found.
    lines.append(
        "Nobody is named on this request, so it sits in the open queue for anyone entitled."
    )
    return OutboundMessage(
        recipient=request.requested_by,
        subject=f"Still unanswered by anyone: {request.subject}",
        body="\n".join(lines),
        kind="awaiting",
        correlation_id=request.correlation_id,
    )


class _OpenInput(BaseModel):
    """The typed argument for `open_pending_request_activity`."""

    request_id: str
    request: AwaitRequest
    # The workflow's clock when it opened the wait. The activity adds the clamped deadline to this,
    # so the timers are scheduled against a value recorded in history.
    started_at: str
    # The Temporal run this projection belongs to, so the store can tell a retry (same run, update
    # in
    # place) from a re-ask after a lapsed deadline (new run, reopen the row).
    run_id: str = ""


class _SettleInput(BaseModel):
    """The typed argument for `settle_pending_request_activity`."""

    request_id: str
    state: str
    answered_by: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


@durable_activity("background")
@activity.defn
async def open_pending_request_activity(payload: _OpenInput) -> str:
    """Project the open wait into `pending_requests`, and return the deadline it was opened with.

    Idempotent within a run and reopening across runs. The clamp against `awaiting_max_days` lives
    here because an activity may read `settings` and no caller can skip it; the result is recorded
    in history, so a replay uses the original deadline even if the ceiling moved.

    Returns:
        The clamped `due_at`, ISO-8601.
    """
    deadline = timedelta(
        days=max(0.0, min(payload.request.deadline_days, settings.awaiting_max_days))
    )
    due_at = datetime.fromisoformat(payload.started_at) + deadline
    # Every terminal state is reopenable by a different run; prior answers are archived in
    # `pending_request_answers`.
    await pending_store.open_request(
        request_id=payload.request_id,
        kind=payload.request.kind,
        subject=payload.request.subject,
        rationale=payload.request.rationale,
        asked_of=payload.request.asked_of,
        requested_by=payload.request.requested_by,
        session_id=payload.request.session_id,
        correlation_id=payload.request.correlation_id,
        premise_note_ids=payload.request.premise_note_ids,
        due_at=due_at,
        run_id=payload.run_id,
    )
    return due_at.isoformat()


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
async def record_reminder_activity(request_id: str, count: int = 0) -> None:
    """Record the asking workflow's escalation count against a still-open request.

    The workflow passes its running total rather than an increment, because an activity is
    at-least-once; `GREATEST` in the store makes a redelivery a no-op. `count` defaults to 0 for
    tasks scheduled by an older worker; the next escalation writes the total and catches up.
    """
    await pending_store.record_reminder(request_id, count)


@durable_workflow("background")
# Failures must be able to fail the workflow rather than park in an unbounded task retry loop, or
# a parent waiting on it waits forever.
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

        Ignored rather than rejected: a signal has no reply channel. The front door refuses a second
        answer with a 409 by reading the store.
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
        # Safe to read `settings` here: it sets an activity timeout, a command attribute replay
        # tolerates, not the number of commands.
        activity_timeout = timedelta(seconds=settings.awaiting_activity_timeout_seconds)

        # The `try` covers the open as well as the wait: a cancellation while the open activity is
        # in
        # flight would otherwise leave a committed `waiting` row nothing will ever settle. Settling
        # a row
        # that was never opened is a no-op.
        try:
            # The activity applies the clamp and returns `due_at`. Computing it here from `settings`
            # would let
            # a changed ceiling alter the number of timers on replay (a non-determinism error), and
            # leaving it
            # to callers lets one skip it.
            opened = await workflow.execute_activity(
                open_pending_request_activity,
                _OpenInput(
                    request_id=request_id,
                    request=request,
                    started_at=workflow.now().isoformat(),
                    run_id=workflow.info().run_id,
                ),
                start_to_close_timeout=activity_timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            due_at = datetime.fromisoformat(opened)
            await self._notify(request, request_id, due_at.isoformat())
            await self._wait_until(due_at, request, request_id)
        except (asyncio.CancelledError, ActivityError) as exc:
            # A cancelled wait must stop saying it is open, so the settle runs with `ABANDON`. A
            # cancellation
            # arrives as `CancelledError` during `wait_condition` and as
            # `ActivityError(cause=CancelledError)`
            # inside an activity; any other activity failure is re-raised and fails the wait.
            if isinstance(exc, ActivityError) and not isinstance(exc.cause, TemporalCancelledError):
                raise
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

        The reminder interval is a timeout on the wait, so an answer mid-interval is seen
        immediately.
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
                # `wait_condition` raises on timeout; the loop and the `due_at` check decide whether
                # this was a
                # reminder tick or the deadline.
                pass
            if self._answer is None and workflow.now() < due_at:
                self._reminders += 1
                await workflow.execute_activity(
                    record_reminder_activity,
                    # The running total, rebuilt identically on replay, so a redelivery writes the
                    # same number.
                    args=[request_id, self._reminders],
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
        """Tell whoever should know: the requester's mailbox, and the channel out.

        A wait with no session is ordinary (Schedule-resumed campaigns, workflow-raised questions);
        `SessionEventInput` would raise in workflow code, so the session push is skipped. The
        outbound
        copy is not skipped: it goes to `asked_of` on the opening notice and every reminder, and to
        the
        requester on expiry (see `_awaiting_message`).
        """
        if request.session_id:
            await notify_session_best_effort(request.session_id, AWAITING_KIND, payload)
        # Behind a patch: runs opened before outbound delivery replay without the marker and take
        # the old
        # path; a new activity here would otherwise be a non-determinism error that fails an open
        # wait.
        # The patch id may never be reused.
        if workflow.patched("awaiting-outbound-delivery"):
            await deliver_best_effort(_awaiting_message(request, payload))

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
