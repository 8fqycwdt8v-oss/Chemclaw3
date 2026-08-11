"""Reading a session's proposed plan from outside a turn (M13 Step 5).

The plan gate reads the plan *during* a call, off `request.state`, which is this turn's live view.
Two callers need the same plan while no turn is running: `api/routes/plan.py`, so a chemist can see
what is about to be executed and approve it, and the CLI's `/plan` and `/approve` commands.

Under MAF both went through an in-process `AgentSession` object — the front door held one per live
session and the harness kept its todo list inside it. That object is gone: `TodoListMiddleware`
owns `todos`, and where it *lives* between turns is the checkpointer, keyed by the session id as
`thread_id`. So the read is a checkpointer read, and this module is the one place that knows that.

**One place, because the identity must not be computed twice.** `plan_identity` hashes the rendered
todo lines, and a durable approval row is keyed on that hash. If the route derived the plan
differently from the gate — a different field, a different order, bookkeeping rows included — a
chemist's approval would hash to something the gate never asks about, and every write would be
refused with the plan visibly approved on screen. That is not hypothetical: it is the shape of the
defect D-167 fixed, where `/approve` recorded against `current_plan_hash` while the guard asked
`todo_titles`, and a session whose list held only bookkeeping recorded an approval against the
empty-plan constant.

**Absent state is not an error.** A session that has never taken a turn has no checkpoint, and a
session mid-first-turn may have one with no todos yet. Both mean "no plan yet", which is what the
route renders and what `/approve` refuses to act on — so this returns an empty list rather than
raising, and the callers decide what nothing means.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def session_todos(session_id: str, *, saver: Any | None = None) -> list[str]:
    """The todo lines a session is currently proposing, newest checkpoint first.

    Args:
        session_id: The session, which is the checkpointer's `thread_id`.
        saver: The checkpointer to read. `None` resolves the configured one — passed in by the
            front door, which already holds it open for the turn path and must not open a second.

    Returns:
        The todo `content` strings in order, or `[]` when the session has no checkpoint, no todos,
        or the checkpointer cannot be reached. The last case is logged: a plan that reads as empty
        because the database hiccuped is indistinguishable to a chemist from one that is empty, and
        the difference decides whether "approve" is even offered.
    """
    checkpoint = await _latest_checkpoint(session_id, saver)
    if checkpoint is None:
        return []
    todos = checkpoint.get("channel_values", {}).get("todos") or []
    return [str(todo["content"]) for todo in todos if isinstance(todo, dict) and "content" in todo]


async def _latest_checkpoint(session_id: str, saver: Any | None) -> dict[str, Any] | None:
    """The most recent checkpoint for `session_id`, or `None` if there is none to read."""
    if saver is None:
        from chemclaw.agent.checkpointer import checkpointer

        try:
            saver = await checkpointer()
        except Exception:  # noqa: BLE001 - a plan read must never fail the request it serves
            logger.warning(
                "could not reach the checkpointer to read session %s's plan; it will render as "
                "having none, which is indistinguishable from a session that has proposed nothing",
                session_id,
                exc_info=True,
            )
            return None
    try:
        tuple_ = await saver.aget_tuple({"configurable": {"thread_id": session_id}})
    except Exception:  # noqa: BLE001 - same rule
        logger.warning("could not read session %s's plan checkpoint", session_id, exc_info=True)
        return None
    return dict(tuple_.checkpoint) if tuple_ is not None else None
