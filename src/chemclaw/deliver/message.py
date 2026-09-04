"""What leaves: a message addressed to a person, and the redaction it passes through first.

**Every field is bounded except two, and those two are redacted on the way out.** `subject` and
`body` are the only free text a channel carries, and a delivered message is the one thing in this
system that reaches a destination the deployment does not fully control — an inbox, a chat room, a
mounted share. `core/logging.py` already resolves every connector bearer-token env-var name so a
credential can be scrubbed from a log line; the same filter runs here, because a message assembled
from a tool result is exactly as capable of carrying one as a log line is, and a log line at least
stays inside the cluster.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field

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
        """
        extra = _connector_secret_envs()
        return self.model_copy(
            update={
                "recipient": redact_secrets(self.recipient, extra_secrets=extra),
                "subject": redact_secrets(self.subject, extra_secrets=extra),
                "body": redact_secrets(self.body, extra_secrets=extra),
            }
        )
