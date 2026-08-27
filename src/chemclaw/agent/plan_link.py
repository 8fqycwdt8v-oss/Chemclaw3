"""Publishing which plan step a tool call serves, without ever writing the plan.

The decision is D-2026-08-27-a-job-names-the-step-it-serves. The old link ran the other way and
was deleted twice: a todo waiting on a durable job was marked by prefixing its `content`, and any
launcher that edits a todo perturbs `plan_identity` and revokes the approval keyed on it. So the
plan is never touched. Instead, this middleware reads the turn's own view of the todo list — the
same `request.state["todos"]` the plan gate enforces
against, so the two cannot disagree about what the plan was — and binds the current step and the
plan's identity as ambient context for the duration of each tool call. A launcher reads them the
way it already reads the ambient session id, and stamps them onto the job it starts.

Attached whenever the harness runs for the profile (not only under `plan_only`): the todo list
exists in `execute` autonomy too, and a job launched there deserves the same join.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.plan_gate import plan_identity, rewrite_todos_in_batch
from chemclaw.core.plan_context import reset_current_plan_link, set_current_plan_link


def plan_link_from_todos(todos: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """The `(plan_step, plan_hash)` a call against this todo list should be stamped with.

    The step is the `content` of the **first `in_progress` todo** — the harness prompt has the
    model mark exactly the step it is working before acting on it, so the first one in flight is
    the step this call serves. No step in flight stamps `""`, which reads as the honest "this call
    was not made from a plan step" rather than a guess at the nearest pending one.

    The hash is `plan_identity` over the bare contents — the same identity the approval row is
    keyed on, computed by the same function, so a stamped job can be matched to the plan revision
    a chemist actually decided about. An empty plan has no identity (`plan_identity` returns
    `None` for the reason its docstring gives), so it stamps `""` too.
    """
    contents = [str(todo["content"]) for todo in todos if "content" in todo]
    in_progress = (
        str(todo["content"])
        for todo in todos
        if todo.get("status") == "in_progress" and "content" in todo
    )
    return next(in_progress, ""), plan_identity(contents) or ""


@wrap_tool_call
async def stamp_plan_link(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Bind the plan link around the tool body, so launchers can read it ambiently.

    Reads `request.state` and writes nothing back — the plan gate's own tests hold that a launch
    leaves `plan_identity` unchanged, and this middleware is why that stays trivially true: the
    link travels on a `contextvar`, not on the state.

    **`request.state["todos"]` is the same pre-batch snapshot `enforce_plan_approval` had to work
    around.** The canonical "tick the completed step, do the next one" batch carries a `write_todos`
    status flip beside the tool call it pairs with — step N marked completed, step N+1 in_progress —
    in the *same* assistant message, so `request.state` still shows step N as `in_progress` when
    this runs. Stamping from it named the step that had just finished, not the one this call
    actually serves. So this reads the batch's own rewrite first, the same way
    `enforce_plan_approval` judges the call against the plan the batch *writes*
    (`rewrite_todos_in_batch`/`plan_after_batch`), and falls back to `request.state` only when the
    batch carries no answerable one — no rewrite in this batch, or one this call cannot make sense
    of (two rewrites gathered concurrently, unparseable arguments).

    An absent `todos` key (a profile without the harness would not attach this at all; a subagent
    has the key stripped) binds the empty link rather than reaching for the checkpoint: the
    fallback would be one superstep stale, statusless, and a second answer to "what was the plan"
    — and the empty stamp is already the defined meaning for "not launched from a step".

    The bind/reset pair wraps the handler in `try/finally` so a raising tool body cannot leak one
    call's link into the next. This sits innermost in the governed chain — inside the plan gate —
    so a refused call never binds a link at all: nothing launched, nothing to stamp.
    """
    batch_todos = rewrite_todos_in_batch(request)
    todos = (
        batch_todos if isinstance(batch_todos, list) else (request.state or {}).get("todos") or []
    )
    step, plan_hash = plan_link_from_todos(todos)
    token = set_current_plan_link(step, plan_hash)
    try:
        return await handler(request)
    finally:
        reset_current_plan_link(token)
