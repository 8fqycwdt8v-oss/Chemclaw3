"""The session handle a turn is run against — an id, and the scratch state around it.

Turn state lives in the LangGraph checkpointer keyed by the session id; this object carries the
id plus an in-process `state` dict. The dict has one writer: the in-memory history provider, which
keeps its transcript there (and the disconnect rollback snapshots it). The durable provider ignores
it, so under `session_store=postgres` it stays empty and a replica that has never seen the session
loses nothing by lacking it. The front door caches one handle per live session so the ownership
gate, the turn claim and the cache refer to the same thing.

Not durable, deliberately: what a session must not lose (owner, plan, conversation, approvals)
is in Postgres.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnSession:
    """One conversation's in-process handle: its id, and the scratch state around it."""

    session_id: str
    # The in-memory history provider's thread, and the runner's rollback snapshot of it; untyped
    # because neither is a schema this module should assert. Never written under Postgres.
    state: dict[str, Any] = field(default_factory=dict)
