"""Outbound delivery from a workflow: the one activity, and the best-effort wrapper.

**Four `Message.kind` values were declared and one was produced.** `deliver/message.py` bounds the
vocabulary to `digest`, `awaiting`, `job-result` and `report` — a `Literal` rather than a
convention,
because the file driver builds a filename out of it — and the only caller in the tree was the
nightly digest. The other three named the three things a chemist most needs to hear about while they
are *not* in a session: a question is waiting on them, a job they launched has finished, a report
they asked for is written. Each of those workflows already pushed back into `session_events`, which
is the mailbox a session reads; none of them could reach anybody who had closed the tab.

So this module is the second half of the push-back seam, deliberately shaped like the first
(`durable/notify.py`): one activity that does the I/O, one workflow-side wrapper that never fails
the job whose real result is already durable. The two are not alternatives and no caller chooses
between them — the mailbox is the durable handover and the channel is the courtesy on top, which is
why `durable/digest.py` advances its watermark on the mailbox and ignores what this returns.

**Why every producer routes through here rather than calling `deliver()` itself.** `deliver()` is
async I/O over a configured driver, so it can only run in an activity; an activity is a registration
and a queue and a timeout triple, and four copies of that is four chances to get the
`schedule_to_start` bound wrong in the way `durable/notify.py` documents at length. The digest's own
activity is what this generalises: it is gone, and its two hard-won properties are kept here —
the enablement check runs *inside* the activity (a workflow that branched on `delivery_enabled()`
would emit a command its replayed history does not contain), and the `Message` is *constructed*
inside it (see `OutboundMessage`).
"""

import asyncio
import logging
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError
from temporalio.exceptions import CancelledError as TemporalCancelledError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import degraded
    from chemclaw.deliver.message import Message
    from chemclaw.deliver.registry import deliver, delivery_enabled
    from chemclaw.durable.publish import (
        BAD_DATA_RETRY,
        activity_failure_reason,
        light_write_queue_wait_timeout,
    )
    from chemclaw.durable.registry import durable_activity

logger = logging.getLogger(__name__)


class OutboundMessage(BaseModel):
    """What a workflow asks to have delivered — deliberately looser than `Message`.

    Every field is a plain `str` with no `min_length`, and `kind` is not `Message`'s `Literal`,
    because **`Message`'s constraints have to fail inside the activity**. A workflow that built a
    `Message` with an empty `recipient` would raise `ValidationError` in *workflow* code, which no
    best-effort wrapper can catch — it guards the activity, not the argument — so the notification
    that must never fail the job would be the thing that fails it. That is not hypothetical: it is
    the bug `AwaitingWorkflow._push` carries a guard for (every sessionless wait failed before it)
    and the one the digest's delivery activity carries a comment for (an empty subscription owner
    aborting every subscriber after it).

    So the `Literal` stays the bound where the bound matters — `Message.kind` is what
    `FileDeliveryDriver` builds a filename out of, and an unbounded value there is an arbitrary file
    write — and this model is the wire. A bad value crossing it is a caught `ValidationError` and a
    counted degradation, not a workflow task that retries forever.
    """

    recipient: str = ""
    subject: str = ""
    body: str = ""
    kind: str = "digest"
    correlation_id: str = ""


@durable_activity("background")
@activity.defn
async def deliver_message_activity(payload: OutboundMessage) -> list[str]:
    """Send one message on every enabled outbound channel, and say which ones took it.

    An activity because it is I/O, and **the enablement check belongs here rather than in the
    workflow**: `delivery_enabled()` reads `settings`, and a workflow that branched on it decided
    whether to emit a command at all — so enabling a channel and restarting a worker made an
    in-flight run replay a command its history does not contain.

    It never raises. `deliver()` already swallows a single channel's failure so one broken webhook
    is not everyone's outage; what is left is a misconfigured seam — a channel named in
    `CHEMCLAW_DELIVERY_CHANNELS` with no folder raises `DeliveryChannelError`, an unaddressable
    message raises `ValidationError` — and both are in `_BAD_DATA_TYPES`, so a raise here would fail
    this activity **non-retryably** and take the caller's run with it. Every caller's real result is
    already durable by the time this runs, so the failure is reported and counted instead.

    Returns:
        The channels that took the message. Empty means either that delivery is off or that every
        channel refused — which the log line distinguishes and a caller cannot.
    """
    if not delivery_enabled():
        return []
    try:
        message = Message(
            recipient=payload.recipient,
            subject=payload.subject,
            body=payload.body,
            kind=payload.kind,  # type: ignore[arg-type]
            correlation_id=payload.correlation_id,
        )
        taken = await deliver(message)
    except Exception as exc:
        degraded(
            logger,
            "message_delivery",
            "outbound delivery of a %s message to %r failed: %s",
            payload.kind,
            payload.recipient,
            exc,
        )
        return []
    if not taken:
        degraded(
            logger,
            "message_delivery",
            "no delivery channel took the %s message for %r",
            payload.kind,
            payload.recipient,
        )
    return taken


async def deliver_best_effort(message: OutboundMessage) -> list[str]:
    """Deliver outbound, and never fail the caller because a channel could not be reached.

    The same discipline as `notify_session_best_effort` and `publish_note_best_effort`, for the same
    reason: the science, the answer or the question is already durable, and a courtesy copy that
    could not be sent must not undo it. The activity itself never raises; what this guards is the
    *scheduling* of it — an unserved `background-jobs` queue, a worker rolling, a broker hiccup.

    **A cancellation is re-raised rather than swallowed**, exactly as
    `D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting` required of the
    session push-back: a workflow cancelled while this is in flight gets
    `ActivityError(cause=CancelledError)`, and reporting that as "the copy was dropped, carry on"
    hides the cancellation from the caller's cleanup clause.

    Returns:
        The channels that took it. Empty covers delivery being off, every channel refusing, and the
        activity never running — a caller that needs to tell those apart is asking the wrong seam.
    """
    if not message.recipient:
        # No addressee is not a failure and not a degradation: a report nobody asked for by name, a
        # wait open to anyone entitled. The `min_length=1` inside would make it one.
        return []
    try:
        return await workflow.execute_activity(
            deliver_message_activity,
            message,
            task_queue=settings.background_task_queue,
            start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
            # The wait and the work are bounded separately for the reason `durable/notify.py`
            # measures at length: `start_to_close` begins only once a worker has picked the task up,
            # so on its own it is not a bound on this call at all.
            schedule_to_start_timeout=light_write_queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
    except ActivityError as exc:
        if isinstance(exc.cause, TemporalCancelledError):
            raise asyncio.CancelledError(
                f"outbound delivery of a {message.kind} message was cancelled with its workflow"
            ) from exc
        workflow.logger.warning(
            "outbound delivery of a %s message failed: %s",
            message.kind,
            activity_failure_reason(exc),
        )
        return []
