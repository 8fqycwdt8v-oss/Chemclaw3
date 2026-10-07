"""Who wrote this: the person it was written for, and the agent that wrote it.

One shape shared by knowledge notes, the audit trail and session transcripts:

- **`actor`**: the human principal (Entra `oid`, `core/identity_context.get_current_actor`).
  `None` means not recorded, never "nobody", and it is never invented.
- **`agent`**: the agent that wrote it, or `None` when a human wrote it directly. A name is the
  writing graph's `AgentProfile`; `UNNAMED_AGENT` (`""`) means an unnamed agent, matching
  `audit_events.agent`'s existing encoding so that table needs no migration.

Always a pair: an agent's act is recorded beside the person, never instead of them
(`D-2026-09-27-an-author-is-a-person-and-an-agent`).
"""

from pydantic import BaseModel, ConfigDict

#: An agent wrote it, and which one is not named — `audit_events.agent`'s meaning of `""`.
UNNAMED_AGENT = ""


class Authorship(BaseModel):
    """The person a thing was written for, and the agent that wrote it (`None`: a human did)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str | None = None
    agent: str | None = None

    @property
    def by_agent(self) -> bool:
        """Whether an agent wrote it — the distinction `Note.created_by` has always carried."""
        return self.agent is not None
