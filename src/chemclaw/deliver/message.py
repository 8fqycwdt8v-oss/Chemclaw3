"""What leaves: a message addressed to a person, and the redaction it passes through first.

**Every field is bounded except two, and those two are redacted on the way out.** `subject` and
`body` are the only free text a channel carries, and a delivered message is the one thing in this
system that reaches a destination the deployment does not fully control — an inbox, a chat room, a
mounted share. `core/logging.py` already resolves every connector bearer-token env-var name so a
credential can be scrubbed from a log line; the same filter runs here, because a message assembled
from a tool result is exactly as capable of carrying one as a log line is, and a log line at least
stays inside the cluster.
"""

from pydantic import BaseModel, Field

from chemclaw.core.logging import redact_secrets


class Message(BaseModel):
    """One delivery: who it is for, what it says, and what caused it.

    `recipient` is an address in the *channel's* namespace — an actor id for one channel, an email
    for another, a room for a third — and this model does not interpret it. Resolving an actor to
    an address is the driver's job, because only the driver knows what an address is there.
    """

    recipient: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = ""
    #: What produced this, so a delivery can be joined back to the run that caused it. A bounded
    #: vocabulary: `digest`, `awaiting`, `job-result`, `report`.
    kind: str = "digest"
    #: The turn or job this came from, for the same join. Never rendered to the recipient.
    correlation_id: str = ""

    def redacted(self) -> "Message":
        """This message with every configured secret scrubbed from its free text.

        Applied by the registry immediately before a driver sees it, rather than by each driver:
        a redaction every driver has to remember is a redaction the next driver forgets, and the
        one that forgets is the one that sends outside the cluster.
        """
        return self.model_copy(
            update={
                "subject": redact_secrets(self.subject),
                "body": redact_secrets(self.body),
            }
        )
