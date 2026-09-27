"""Who wrote this: the person it was written for, and the agent that wrote it.

**One answer to one question, taken once** (`D-2026-09-27-an-author-is-a-person-and-an-agent`).
Three subsystems record something somebody wrote — a knowledge note, a tool call on the audit trail,
a message in a session's transcript — and each had noticed, in its own corner, that it could not
say who. Answered three times they would have shipped three subtly different answers, so the shape
lives here and each subsystem spells its storage in the same two names:

- **`actor`** — the human principal on whose behalf it was written: the Entra `oid` the turn or job
  ran under (`core/identity_context.get_current_actor`). `None` means *not recorded*, never
  "nobody": a row written before this existed, or off the request path where no person is bound.
  It is never invented.
- **`agent`** — the agent that wrote it, or `None` when **a human wrote it directly**. A name is
  the `AgentProfile` of the graph that wrote it (a helper's derived `<caller>-helper`, a peer's
  own name); `UNNAMED_AGENT` (`""`) is *an agent wrote it and which one is not named*.

**Why `""` is the unnamed agent rather than a sentinel word.** It is what `audit_events.agent`
has meant since `D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail`: the trail names the
human always and the agent only when it is not the one being spoken to. Taking the audit trail's
encoding as the shared one is what lets that table join the model with no migration and no
backfill, and a note or a transcript row that says "an agent wrote this" without saying which is
exactly that statement. A word such as `"agent"` would have been a second spelling of it, and a
profile could one day be called that.

**Beside the person, never instead of them** — `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-
actor`, invariant 3. An agent's act recorded under a person's identity alone is the D-040 failure,
which is why this is a *pair* and not one column that holds whichever is more interesting.
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
