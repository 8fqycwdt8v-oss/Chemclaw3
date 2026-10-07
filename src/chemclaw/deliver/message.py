"""What leaves: a message addressed to a person, and the redaction it passes through first.

A delivered message reaches a destination the deployment does not fully control (an inbox, a
chat room, a mounted share), so every free-text field — `recipient`, `subject`, `body` and
attachment content — is scrubbed of secrets, including connector bearer tokens, on the way out.
"""

import base64
import binascii
import logging
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer, PlainValidator

from chemclaw.core.logging import redact_secrets
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)


def _connector_secret_envs() -> tuple[str, ...]:
    """The connector bearer-token variable names, or `()` if they cannot be resolved.

    Shared with `core.logging.SecretRedactingFilter` through
    `connectors.registry.bearer_token_env_names`, so the log scrub and the delivery scrub cover the
    same set. Failure degrades to redacting nothing extra rather than blocking a delivery, and is
    reported because this path leaves the cluster.
    """
    try:
        from chemclaw.connectors.registry import bearer_token_env_names

        return bearer_token_env_names()
    except Exception:
        # `degraded()` counts `chemclaw_degraded_total{subsystem}`, which is alerted.
        degraded(
            logger,
            "deliver_redaction",
            "connector bearer-token names could not be resolved; connector credentials will NOT "
            "be scrubbed from outbound messages",
        )
        return ()


def _decode(value: Any) -> bytes:
    """Take an attachment's bytes off the wire, accepting base64 text or bytes already."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachment content is not valid base64: {exc}") from exc
    raise ValueError(f"attachment content must be bytes or base64 text, not {type(value).__name__}")


#: An attachment's bytes, base64 on the wire in both directions. The message crosses a Temporal
#: activity boundary, and pydantic's default `bytes` handling (utf-8 decode) would corrupt or
#: reject binary content in a durable payload.
AttachmentBytes = Annotated[
    bytes,
    PlainValidator(_decode),
    PlainSerializer(lambda value: base64.b64encode(value).decode("ascii"), return_type=str),
]

#: What a filename may be: no separator, no `..`, no leading dot, so an attachment cannot escape
#: an outbox, hide itself, or overwrite the message file it sits beside.
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Attachment(BaseModel):
    """One file travelling with a message: what it is called, what it is, and its bytes.

    **The seam exists because a pointer is not a deliverable.** A report reached a chemist who had
    closed the tab as "recorded as `report-…`; open it beside its citations" — a note id, which is
    deliverable only to somebody who can already reach the graph. The document they asked for is
    the thing they wanted, and it has a name and a type.

    `filename` is bounded rather than documented, for the reason `Message.kind` is: the file driver
    writes it into a directory, so an absolute or `../`-bearing value is an arbitrary file write
    with the pod's uid. `media_type` is the receiver's problem to honour and this model does not
    interpret it.
    """

    filename: str = Field(min_length=1, max_length=128, pattern=_FILENAME.pattern)
    media_type: str = Field(default="text/plain", min_length=1, max_length=128)
    content: AttachmentBytes = b""

    model_config = {"frozen": True}


class Message(BaseModel):
    """One delivery: who it is for, what it says, and what caused it.

    `recipient` is an address in the *channel's* namespace — an actor id for one channel, an email
    for another, a room for a third — and this model does not interpret it. Resolving an actor to
    an address is the driver's job, because only the driver knows what an address is there.
    """

    recipient: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = ""
    #: What produced this, so a delivery can be joined back to the run that caused it. A `Literal`
    #: because the file driver builds a filename from it; an arbitrary string could escape the
    #: outbox.
    kind: Literal["digest", "awaiting", "job-result", "report", "work-check-in"] = "digest"
    #: The turn or job this came from, for the same join. Never rendered to the recipient.
    correlation_id: str = ""
    #: The files this message carries. Bounded, because a driver writes them outside the cluster.
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)

    def redacted(self) -> "Message":
        """This message with every configured secret scrubbed from its free text.

        Applied by the registry immediately before a driver sees it, so no driver can forget it.
        Covers `recipient` too, since drivers put it where they put the body. A rewritten
        `recipient` is logged (scrubbed form only), because scrubbing an address can re-address a
        message to nowhere; a redaction in `subject` or `body` only costs words.
        """
        extra = _connector_secret_envs()
        recipient = redact_secrets(self.recipient, extra_secrets=extra)
        if recipient != self.recipient:
            logger.warning(
                "the outbound redaction rewrote this message's recipient to %r; a driver will "
                "resolve that address rather than the one it was given, and an address that no "
                "longer routes is a message nobody receives",
                recipient,
            )
        return self.model_copy(
            update={
                "recipient": recipient,
                "subject": redact_secrets(self.subject, extra_secrets=extra),
                "body": redact_secrets(self.body, extra_secrets=extra),
                "attachments": [_redacted_attachment(one, extra) for one in self.attachments],
            }
        )


def _redacted_attachment(attachment: Attachment, extra: tuple[str, ...]) -> Attachment:
    """The same scrub over an attachment's bytes, where they decode as text.

    Bytes that are not utf-8 pass through untouched rather than failing the delivery: a credential
    inside a binary attachment is not scrubbed.
    """
    try:
        text = attachment.content.decode("utf-8")
    except UnicodeDecodeError:
        return attachment
    scrubbed = redact_secrets(text, extra_secrets=extra)
    if scrubbed == text:
        return attachment
    return attachment.model_copy(update={"content": scrubbed.encode("utf-8")})
