"""The session handle a turn is run against — an id, and the scratch state around it.

This is what MAF's `AgentSession` was used for here, reduced to what the front door actually asked
of it. The framework's version carried a thread, a history provider and a per-run message store;
none of that is true any more. Turn state lives in the LangGraph checkpointer, keyed by the session
id as `thread_id`, and the transcript is a projection written once per turn — so what is left is
the id, plus a dict for the handful of per-session facts the front door itself keeps.

**Why a real object rather than passing the id around.** Two reasons, and the first is the one that
would bite:

- `state` is genuinely per-session and genuinely in-process. It is the bag the disconnect rollback
  snapshots and restores (`chemclaw.api.runner`), and it is what the in-memory history provider
  writes its thread into. A dict created per call could not be either.
- The front door caches one of these per live session (`api.state.LiveSessions`), and giving it a
  named type is what lets the ownership gate, the turn claim and the cache all be about the same
  thing rather than about three strings that happen to be equal.

**Not durable, deliberately.** Everything here dies with the pod, and that is now correct rather
than tolerated: what a session must not lose — its owner, its plan, its conversation, its approvals
— is in Postgres. Before the rebuild it was not, and a rehydrated handle proposing an empty plan
meeting its own already-spent approval was the cost.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnSession:
    """One conversation's in-process handle: its id, and the scratch state around it."""

    session_id: str
    # Per-session scratch, owned by whoever writes into it. Untyped on purpose: the two writers are
    # the in-memory history provider (its thread) and the runner's rollback snapshot (everything),
    # and neither is a schema this module should be asserting. The *durable* state that used to
    # live here is typed, in `chemclaw.agent.state.ChemclawState`, because the graph declares it.
    state: dict[str, Any] = field(default_factory=dict)
