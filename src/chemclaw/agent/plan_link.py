"""Publishing which plan step a tool call serves, without ever writing the plan.

Editing a todo would perturb `plan_identity` and revoke the approval keyed on it, so this middleware
only reads the turn's todo list (via `plan_link_for_call`, the same reading `agent/audit.py` uses)
and binds the current step and plan identity as ambient context around each tool call. Launchers
stamp them onto the jobs they start (D-2026-08-27-a-job-names-the-step-it-serves). Attached whenever
the harness runs, in every autonomy mode.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.plan_gate import plan_identity, rewrite_todos_in_batch
from chemclaw.core.plan_context import reset_current_plan_link, set_current_plan_link


def plan_link_from_todos(todos: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """The `(plan_step, plan_hash)` a call against this todo list should be stamped with.

    The step is the `content` of the first `in_progress` todo, or `""` when none is in flight. The
    hash is `plan_identity` over the whole steps (content and declaration), the identity approvals
    are keyed on; an empty plan stamps `""`.
    """
    steps = [todo for todo in todos if "content" in todo]
    in_progress = (
        str(todo["content"])
        for todo in todos
        if todo.get("status") == "in_progress" and "content" in todo
    )
    return next(in_progress, ""), plan_identity(steps) or ""


def plan_link_for_call(request: Any) -> tuple[str, str]:
    """The `(plan_step, plan_hash)` for one tool call, shared by the job stamp and the audit row.

    `request.state["todos"]` is the snapshot taken before the batch, and a batch usually carries a
    `write_todos` status flip beside the call it pairs with; read off the state, the stamp would
    name the step that just finished. So the batch's own rewrite is read first, and `request.state`
    is the fallback when the batch has none or it cannot be interpreted.
    """
    batch_todos = rewrite_todos_in_batch(request)
    todos = (
        batch_todos if isinstance(batch_todos, list) else (request.state or {}).get("todos") or []
    )
    return plan_link_from_todos(todos)


@wrap_tool_call
async def stamp_plan_link(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Bind the plan link around the tool body, so launchers can read it ambiently.

    Writes nothing back to state; the link travels on a `contextvar`. An absent `todos` key binds
    the empty link. The bind/reset is in `try/finally` so a raising tool cannot leak one call's link
    into the next. This sits innermost, inside the plan gate, so a refused call never binds a link.
    """
    step, plan_hash = plan_link_for_call(request)
    token = set_current_plan_link(step, plan_hash)
    try:
        return await handler(request)
    finally:
        reset_current_plan_link(token)
