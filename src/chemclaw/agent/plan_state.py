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


async def session_todos(session_id: str, *, saver: Any | None = None) -> list[str] | None:
    """The todo lines a session is currently proposing, or `None` when the plan is unreadable.

    **`None` and `[]` are different answers and the callers act on them differently**, which is the
    whole reason this does not return one list. `[]` means "this session has proposed nothing" — a
    fact. `None` means "the plan could not be read": no checkpoint, an unreachable checkpointer, or
    a checkpoint whose shape this does not recognise.

    **Only one of the two keys read here is an unpromised literal, and it is not the one this
    docstring used to name first.** `channel_values` is a declared field of
    `langgraph.checkpoint.base.Checkpoint`, a public `TypedDict`, so reading it is API use rather
    than a reach into an internal — the earlier wording put it beside `todos` as if a bump could
    move either. What upstream genuinely never promised is `todos`, which is `TodoListMiddleware`'s
    own state key; that one is pinned by
    `tests/test_upstream_surface.py::test_the_todo_middleware_still_names_the_plan_channel_todos`,
    so a rename is a red build rather than a gate that silently reads every plan as empty.

    Collapsing the two was a fail-*open*: `consume_turn_approval` hashes what this returns, and on
    `[]` it found no decision and returned early **without spending the approval** — leaving a
    one-shot human approval live for every later turn. Its sibling `enforce_plan_approval` fails
    closed on the same input. One input, two opposite directions, is exactly the divergence a gate
    must not have.

    Args:
        session_id: The session, which is the checkpointer's `thread_id`.
        saver: The checkpointer to read. `None` resolves the configured one — passed in by the
            front door, which already holds it open for the turn path and must not open a second.

    Returns:
        The todo `content` strings in order; `[]` for a readable session proposing nothing; `None`
        when the plan could not be read at all.
    """
    checkpoint = await _latest_checkpoint(session_id, saver)
    if checkpoint is None:
        return None
    values = checkpoint.get("channel_values")
    if not isinstance(values, dict):
        logger.warning(
            "session %s's checkpoint carries no readable channel_values; treating its plan as "
            "unreadable rather than as empty",
            session_id,
        )
        return None
    todos = values.get("todos") or []
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
