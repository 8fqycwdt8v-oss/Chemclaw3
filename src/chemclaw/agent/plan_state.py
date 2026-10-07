"""Reading a session's proposed plan from outside a turn.

`api/routes/plan.py` and the CLI's `/plan` and `/approve` need the plan while no turn runs.
`TodoListMiddleware` owns `todos` and the checkpointer holds them between turns (keyed by session id
as `thread_id`), so this is a checkpointer read, in one place so the plan an approval is hashed over
is the same plan the gate checks. Absent state means "no plan yet", not an error.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def session_plan(session_id: str, *, saver: Any | None = None) -> list[dict[str, Any]] | None:
    """The session's plan steps whole, or `None` when the plan is unreadable.

    `[]` means the session proposed nothing; `None` means the plan could not be read (no checkpoint,
    unreachable checkpointer, unrecognised shape). Callers must treat them differently:
    `consume_turn_approval` must not skip spending an approval because a read failed. Steps are
    returned whole because the identity hashes both content and `tools`; steps without readable
    `content` are dropped here so every caller sees the same list. The `todos` channel name is
    pinned by `tests/test_upstream_surface.py`. `saver=None` resolves the configured checkpointer.
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
    return [todo for todo in todos if isinstance(todo, dict) and "content" in todo]


async def _latest_checkpoint(session_id: str, saver: Any | None) -> dict[str, Any] | None:
    """The most recent checkpoint for `session_id`, or `None` if there is none to read."""
    if saver is None:
        from chemclaw.agent.checkpointer import checkpointer

        try:
            saver = await checkpointer()
        except Exception:
            logger.warning(
                "could not reach the checkpointer to read session %s's plan; it will render as "
                "having none, which is indistinguishable from a session that has proposed nothing",
                session_id,
                exc_info=True,
            )
            return None
    try:
        tuple_ = await saver.aget_tuple({"configurable": {"thread_id": session_id}})
    except Exception:
        logger.warning("could not read session %s's plan checkpoint", session_id, exc_info=True)
        return None
    return dict(tuple_.checkpoint) if tuple_ is not None else None
