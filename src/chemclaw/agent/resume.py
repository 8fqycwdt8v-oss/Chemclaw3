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
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from chemclaw.agent.state import turn_config
from chemclaw.core.logging import log_event

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


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """What a resume attempt did, which is not the same as what the graph returned.

    `ainvoke(None, ...)` answers "here is the state" for a thread it resumed, a thread it found
    finished and a thread it had nothing to do with, so the graph's return value cannot carry this.
    A supervisor reads `resumed` to decide whether anything happened and `state` to decide whether
    to try again later.
    """

    state: Resumability
    #: Whether the graph was actually driven. False for every state but `RESUMABLE`.
    resumed: bool


async def resume_turn(
    graph: Any,
    session_id: str,
    *,
    claims: Any,
    holder: str,
    lease_seconds: float,
) -> ResumeOutcome:
    """Continue a turn whose pod died, under the same lease a chat turn holds.

    **The claim is taken *before* the state is read, and the order is the whole correctness
    argument.** Reading first and claiming second leaves a window in which the thread's state
    changes between the two — another process finishing it, or starting a fresh turn on it — and
    the resume then drives a graph it has an out-of-date opinion about. Claiming first makes the
    read happen under exclusion, which is what `SessionTurnClaims` is for: one short statement, no
    pinned connection, and a lease that lapses if this process dies mid-resume.

    Without it, a second writer does not conflict — it **forks**. Measured in Wave 2: two writers on
    one thread produced 26 checkpoint rows with duplicate step numbers under different parents, no
    error, last writer winning, and one pod's answer returned to its caller and absent from the
    session. The checkpointer offers no optimistic concurrency here, so the lease is not a nicety.

    The lease is released in a `finally`, because a resume that raises must not leave the session
    unusable until the lease lapses — the failure a chemist sees would be "your session is busy"
    for a turn nobody is running.

    Args:
        graph: A compiled graph over the checkpointer holding the thread.
        session_id: The session, which is the thread id.
        claims: A `SessionTurnClaims`, or anything with its three-method shape.
        holder: This process's claim identity, as `api/state.claim_holder` builds one.
        lease_seconds: How long the claim is good for before it lapses.

    Returns:
        What happened, which the graph's own return value cannot say.
    """
    if not await claims.claim(session_id, holder, lease_seconds):
        return ResumeOutcome(state=Resumability.HELD, resumed=False)
    try:
        state = await resumability(graph, session_id)
        if state is not Resumability.RESUMABLE:
            log_event(
                logger,
                "turn.resume_skipped",
                "session %s was not resumable (%s)",
                session_id,
                state,
                session_id=session_id,
                resumability=str(state),
            )
            return ResumeOutcome(state=state, resumed=False)
        await graph.ainvoke(None, turn_config(session_id))
        log_event(
            logger,
            "turn.resumed",
            "session %s was resumed after its previous run ended without finishing",
            session_id,
            session_id=session_id,
        )
        return ResumeOutcome(state=Resumability.RESUMABLE, resumed=True)
    finally:
        await claims.release(session_id, holder)
