"""Picking a turn up after the pod that was running it died.

**The durability this module needs already existed and nothing called it.**
`D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up` measured that a fresh
process with a fresh graph and a fresh checkpointer resumes a killed turn with
`ainvoke(None, turn_config(thread_id))` — three kill points, each returning a correct message list
with one tool call, one matching `ToolMessage`, no duplicates and no orphans, and no `interrupt()`
anywhere. So this file is not a durability mechanism; `api/detach.py` runs the turn and the
checkpointer holds it. What is here is the part a caller cannot get right by itself.

## Why a resume is not just that one call

**Two writers on one thread fork the DAG, silently.** Measured with two fresh turns started ~50 ms
apart from two processes on one `thread_id`: no error, no warning, 26 checkpoint rows with duplicate
step numbers under different parents, one pod's question leaking into the other's context, and the
first pod's answer, tool call and tool result **absent from the thread's tip** — returned to its
caller and in no session. The checkpointer offers no optimistic concurrency here: a second writer
forks rather than conflicting. So a resume must hold the same lease a chat turn holds, and
`SessionTurnClaims` is that lease.

**And "resume it" is three questions, not one.** `ainvoke(None, ...)` on a *finished* thread is a
silent no-op that returns the completed state with zero model calls, so a caller cannot tell "I
picked it up and finished it" from "there was nothing to pick up" by the return value; on an
*unknown* thread it raises `EmptyInputError`. Neither is a failure and neither is a resume, and a
supervisor that treated all three alike would re-run finished work and report phantom recoveries.

`Resumability` is those states named, decided before anything is invoked, so the decision is
inspectable and testable without driving a model.
"""

import logging
from enum import StrEnum
from typing import Any

from chemclaw.agent.state import turn_config

logger = logging.getLogger(__name__)


class Resumability(StrEnum):
    """What a thread is, before anything tries to continue it.

    A `StrEnum` so a log line and a metric label read as the word rather than as an ordinal, the
    way every other bounded vocabulary in this tree is spelled.
    """

    #: Work is pending on this thread and nobody else holds it. The only state that resumes.
    RESUMABLE = "resumable"
    #: The thread exists and has nothing left to do. Resuming would be a silent no-op.
    FINISHED = "finished"
    #: No checkpoint under this id. `ainvoke(None, ...)` would raise `EmptyInputError`.
    UNKNOWN = "unknown"
    #: Somebody else holds the turn lease. Resuming would fork the DAG rather than continue it.
    HELD = "held"


async def resumability(graph: Any, session_id: str) -> Resumability:
    """Whether this thread has work pending, is done, or was never started.

    Read off the graph's own state rather than by querying the checkpoint tables, because `next` is
    exactly the question — LangGraph reports the nodes it would run, which is empty for a thread
    that finished and non-empty for one killed with a task pending. Asking SQL would be a second
    implementation of the scheduler's own answer.

    This does **not** consider the lease; `HELD` is the caller's to establish, because taking a
    lease is a side effect and this function is a read.

    Args:
        graph: A compiled graph over the checkpointer holding the thread.
        session_id: The session, which is the thread id.

    Returns:
        `RESUMABLE`, `FINISHED` or `UNKNOWN` — never `HELD`.
    """
    snapshot = await graph.aget_state(turn_config(session_id))
    if snapshot is None or not getattr(snapshot, "created_at", None):
        return Resumability.UNKNOWN
    return Resumability.RESUMABLE if snapshot.next else Resumability.FINISHED
