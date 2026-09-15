"""What leaves: a message addressed to a person, and the redaction it passes through first.

**Every free-text field is redacted on the way out, and there are three of them, not two.** That
sentence read "every field is bounded except two… `subject` and `body` are the only free text a
channel carries" while `redacted()` 140 lines below already said otherwise in as many words —
"every free-text field, `recipient` included. It was skipped, and it is free text by construction"
— and `_redacted_attachment` scrubs `Attachment.content` besides. `recipient` also carries
`min_length=1` with no `max_length`, so it is not bounded in the sense that sentence used either.
A delivered message is the one thing in this system that reaches a destination the deployment does
not fully control — an inbox, a chat room, a mounted share. `core/logging.py` already resolves every connector bearer-token env-var name so a
credential can be scrubbed from a log line; the same filter runs here, because a message assembled
from a tool result is exactly as capable of carrying one as a log line is, and a log line at least
stays inside the cluster.
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

    Imported lazily and shared with `core.logging.SecretRedactingFilter` through
    `connectors.registry.bearer_token_env_names`, so the log scrub and the delivery scrub cannot
    cover different sets. This file used to claim they were already the same ("the same filter runs
    here") — they were not: `redact_secrets` reaches connector tokens only through its
    `extra_secrets` argument, which nothing here passed. A tool error quoting its own
    `Authorization` header was scrubbed from the log line and shipped verbatim to the webhook host.

    Failure degrades to redacting nothing *extra* rather than blocking a delivery, matching the
    filter — but unlike the filter this is on a path that leaves the cluster, so the caller logs it.
    """
    try:
        from chemclaw.connectors.registry import bearer_token_env_names

        return bearer_token_env_names()
    except Exception:
        # `degraded()` rather than a bare `logger.error`, matching the sibling this was extracted
        # from: it increments `chemclaw_degraded_total{subsystem}`, which is alerted and
        # dashboarded. A bare log line here would have made this the *only* security degradation in
        # the tree with no counter — on the half that leaves the cluster, which this module's own
        # docstring calls the more consequential of the two.
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


#: An attachment's bytes, base64 on the wire in **both** directions.
#:
#: Not a bare `bytes`, and the reason is this repository's own replay break. `OutboundMessage`
#: crosses a Temporal activity boundary, so the encoding is part of a durable payload rather than
#: an implementation detail — and pydantic's default for `bytes` is a utf-8 *decode*, which raises
#: on the first byte outside it and silently rewrites the ones inside. A seam that shipped text-only
#: and widened later would be changing the wire under open histories, which is exactly the class of
#: defect `D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes` records.
#: Base64 costs 33% on a webhook POST; measured, a full 96-well run sheet is ~6 kB, so the cost is
#: two kilobytes on the largest artefact this system currently produces.
AttachmentBytes = Annotated[
    bytes,
    PlainValidator(_decode),
    PlainSerializer(lambda value: base64.b64encode(value).decode("ascii"), return_type=str),
]

#: What a filename may be. A driver puts this on a filesystem or hands it to a receiver that will,
#: so the same argument as `Message.kind`: the type is the bound, and prose is not. No separator,
#: no `..`, no leading dot — an attachment cannot escape an outbox, hide itself, or overwrite a
#: message file it sits beside.
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
    #: What produced this, so a delivery can be joined back to the run that caused it.
    #:
    #: **A `Literal` rather than a documented convention, because the file driver builds a filename
    #: out of it.** This said "a bounded vocabulary" in prose and was a bare `str`, while
    #: `FileDeliveryDriver` does `self.directory / f"{message.kind}-{identity}{self.suffix}"` — and
    #: an absolute or `../`-bearing value escapes the outbox entirely, with `mkdir(parents=True)`
    #: creating whatever it traverses to. Inert while the single caller passes a literal, and an
    #: arbitrary file write with the pod's uid the moment a `kind` is ever derived from a payload.
    #: The type is the bound; the prose was not.
    kind: Literal["digest", "awaiting", "job-result", "report"] = "digest"
    #: The turn or job this came from, for the same join. Never rendered to the recipient.
    correlation_id: str = ""
    #: The files this message carries. **Bounded, because a delivery leaves the cluster**: a driver
    #: writes them to a share or POSTs them, and an unbounded list is an unbounded write with the
    #: pod's uid at a destination this deployment does not fully control. `body` stays the message
    #: and an attachment is the artefact — a chemist who cannot open the graph still gets the file.
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)

    def redacted(self) -> "Message":
        """This message with every configured secret scrubbed from its free text.

        Applied by the registry immediately before a driver sees it, rather than by each driver:
        a redaction every driver has to remember is a redaction the next driver forgets, and the
        one that forgets is the one that sends outside the cluster.

        **Every free-text field, `recipient` included.** It was skipped, and it is free text by
        construction — resolving an address is the driver's job, so this model cannot constrain its
        shape — while both shipped drivers put it exactly where the body goes: the file driver
        writes it into the file, the webhook driver POSTs it. Today's only caller passes an actor
        id, so nothing carries a credential there yet; the point of a seam-level scrub is that the
        guarantee does not depend on who is calling it.

        `_connector_secret_envs()` is resolved once rather than per field: it reaches
        `connectors.registry`, and asking it three times per message would import and re-derive the
        bundle set three times for an answer that cannot differ between two fields of one message.

        **A rewritten `recipient` is logged, because scrubbing an address re-addresses a message.**
        `redact_secrets` rewrites *structural* shapes as well as this deployment's own secret
        values, and a routable address can be one: a Teams channel URN loses its token to the
        `TOKEN=` pattern, a webhook URL with userinfo loses its password, a `xoxb-`-shaped address
        is replaced whole. Keeping the scrub is right — a driver puts this field where it puts the
        body, so the guarantee must not depend on the caller — but the failure it can cause is a
        message delivered nowhere, and the driver reporting it can only name the address it was
        given. `subject` and `body` are not logged on the same terms: a redaction there costs
        words, not a destination.

        The line carries the *scrubbed* address only. What tripped the pattern may be a real
        credential, and this is the half of the redaction that leaves the cluster.
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

    **An attachment is free text that leaves the cluster, which is the whole premise of the scrub
    above** — and skipping it because the field is typed `bytes` would put the one field a driver
    writes to a share *outside* the guarantee `redacted()` exists to give. The two artefacts this
    seam carries today (a report's Markdown, a run sheet's CSV) are text and are assembled from
    tool results, exactly as a body is.

    Bytes that are not utf-8 pass through untouched rather than failing the delivery:
    `redact_secrets` works on text, a binary attachment has no text for it to scrub, and one that
    *raises*
    turns the courtesy copy into the thing that fails the job. That is a stated limit rather than a
    silent one — a credential inside a future binary artefact is not scrubbed, and the first such
    producer is when that becomes a decision rather than a note.
    """
    try:
        text = attachment.content.decode("utf-8")
    except UnicodeDecodeError:
        return attachment
    scrubbed = redact_secrets(text, extra_secrets=extra)
    if scrubbed == text:
        return attachment
    return attachment.model_copy(update={"content": scrubbed.encode("utf-8")})
