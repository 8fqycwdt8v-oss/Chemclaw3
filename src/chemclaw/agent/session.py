"""The session handle a turn is run against — an id, and the scratch state around it.

Turn state lives in the LangGraph checkpointer keyed by the session id; this object carries the
id plus an in-process `state` dict (the disconnect rollback's snapshot target and the in-memory
history provider's thread). The front door caches one per live session so the ownership gate,
the turn claim and the cache refer to the same thing.

Not durable, deliberately: what a session must not lose (owner, plan, conversation, approvals)
is in Postgres.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnSession:
    """One conversation's in-process handle: its id, and the scratch state around it."""

    session_id: str
    # Per-session scratch, owned by its writers (the in-memory history provider and the runner's
    # rollback snapshot); untyped because neither is a schema this module should assert.
    state: dict[str, Any] = field(default_factory=dict)
