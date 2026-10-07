"""Outbound delivery from a workflow: the one activity, and the best-effort wrapper.

The second half of the push-back seam, shaped like `durable/notify.py`: the mailbox
(`session_events`) is the durable handover, and this channel copy reaches a chemist who has
closed the tab. Every producer (`digest`, `awaiting`, `job-result`, `report`, `work-check-in`)
routes through here so the activity's timeouts are set once.

The enablement check and the `Message` construction run inside the activity, because a workflow
branching on `delivery_enabled()` would break replay. The cost: with delivery off, each notice
still schedules one activity that returns `[]` — a bounded light write on `background-jobs`.
`deliver_digest_activity` survives in `digest.py` as a replay shim for open runs.
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
    from chemclaw.deliver.message import Attachment, AttachmentBytes, Message
    from chemclaw.deliver.registry import deliver, delivery_enabled
    from chemclaw.durable.publish import (
        BAD_DATA_RETRY,
        activity_failure_reason,
        light_write_queue_wait_timeout,
    )
    from chemclaw.durable.registry import durable_activity

logger = logging.getLogger(__name__)


class OutboundAttachment(BaseModel):
    """One file a workflow asks to have delivered — loose for the same reason `OutboundMessage` is.

    `Attachment.filename` carries a pattern that keeps a file inside its outbox, and that bound has
    to fail *inside* the activity: a workflow constructing an `Attachment` with a rejected filename
    would raise `ValidationError` in workflow code, where no best-effort wrapper can catch it, and
    the courtesy copy would fail the job whose real result is already durable.

    `content` keeps `AttachmentBytes` rather than a bare `bytes`, because that annotation is about
    the *wire* rather than about validation — a workflow passes real bytes and its validator returns
    them unchanged, while the serialiser is what stops Temporal's JSON converter from utf-8-decoding
    a payload it cannot decode.
    """

    filename: str = ""
    media_type: str = "text/plain"
    content: AttachmentBytes = b""


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
    attachments: list[OutboundAttachment] = []


@durable_activity("background")
@activity.defn
async def deliver_message_activity(payload: OutboundMessage) -> list[str]:
    """Send one message on every enabled outbound channel, and say which ones took it.

    The enablement check is here, not in the workflow, so changing it cannot break replay. Never
    raises: a misconfigured seam or unaddressable message would otherwise fail non-retryably and
    take the caller's already-durable run with it; it is logged and counted instead.

    Returns:
        The channels that took the message. Empty means delivery is off, the message or seam was
        bad (counted), or every channel refused (counted); no caller acts differently on these.
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
            attachments=[
                Attachment(
                    filename=one.filename,
                    media_type=one.media_type,
                    content=one.content,
                )
                for one in payload.attachments
            ],
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

    Guards the scheduling of the activity (unserved queue, rolling worker, broker hiccup). It bounds
    the failure, not the delay: the caller can still be held for the schedule-to-start wait plus
    the retry budget. A cancellation is re-raised so the caller's cleanup clause sees it.

    Returns:
        The channels that took it. Empty covers delivery being off, every channel refusing, and the
        activity never running.
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
            # `delivery_timeout_seconds`: the walk over channels is serial and each carries its own
            # network timeout.
            start_to_close_timeout=timedelta(seconds=settings.delivery_timeout_seconds),
            # The wait is bounded separately: `start_to_close` begins only once a worker picks the
            # task up.
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
