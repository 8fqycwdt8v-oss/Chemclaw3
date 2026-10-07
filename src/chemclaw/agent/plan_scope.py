"""What a plan step is allowed to call, declared by the step and approved with it.

An approval must bound what the approved plan may do
(D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool). Only the model knows which
tools carry out a step, so `write_todos` takes a required `tools` list beside `content` and
`status`; a call without it fails argument validation and the model rewrites it. An empty list
means the step changes nothing.

A declaration is not an authorization: `declared_scope` is read only to record what a human
approved, and the gate reads the recorded scope from `plan_approvals`. `plan_identity` hashes each
step's content and declaration (via `step_declaration`), so a rewrite that widens `tools` is a
different plan that must be approved again, while a status flip hashes identically.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, model_validator
from typing_extensions import TypedDict

from chemclaw.core.config import settings
from chemclaw.core.model_prose import ModelProse

# The key a step's declaration is spelled with, in the schema, the state channel and both readers.
TOOLS_FIELD: Final = "tools"


class ScopedTodo(TypedDict):
    """One plan step: what it is, where it has got to, and what it will call.

    `content` and `status` are upstream's `Todo` verbatim — restated rather than inherited because
    a `TypedDict` subclass cannot make an inherited key required differently, and because this is
    the schema the model is shown. Both are pinned against upstream in
    `tests/test_upstream_surface.py`, so a divergence is a red build rather than a plan whose
    status flips stop being recognised.
    """

    content: str
    status: Literal["pending", "in_progress", "completed"]
    tools: list[str]


class ScopedWriteTodosInput(BaseModel):
    """The `write_todos` argument schema — upstream's, with each step declaring its tools.

    **Bounded here rather than in `ScopedTodo`**, and the reason is mechanical: `ScopedTodo` is a
    `TypedDict` whose annotations are evaluated when the class is defined, so an
    `Annotated[..., Field(max_length=...)]` could only carry a literal — and this repository's rule
    is that a threshold comes from the one settings object, ENV-overridable. A validator reads the
    setting at validation time, which is also the only form that can name the offending step.
    """

    todos: list[ScopedTodo]

    @model_validator(mode="after")
    def _a_plan_is_bounded_in_both_directions(self) -> "ScopedWriteTodosInput":
        """Refuse a plan longer, or a step broader, than the configured bound.

        Both sizes outlive the call (the approval row and its recorded scope). Refused at argument
        validation with a message naming the step and count, so the model can split the plan.

        Raises:
            ValueError: The plan declares more steps than `plan_max_steps`, or a step names more
                tools than `plan_max_tools_per_step`.
        """
        if len(self.todos) > settings.plan_max_steps:
            raise ValueError(
                f"this plan has {len(self.todos)} steps and at most {settings.plan_max_steps} are "
                "accepted. Write the plan for the part you are doing now; a plan a person cannot "
                "read is not one they can approve."
            )
        for position, todo in enumerate(self.todos, start=1):
            declared = todo.get("tools", [])
            if len(declared) > settings.plan_max_tools_per_step:
                raise ValueError(
                    f"step {position} declares {len(declared)} tools and at most "
                    f"{settings.plan_max_tools_per_step} are accepted per step. Split it into "
                    "steps that each name the tools they will actually call."
                )
        return self


# What the model is told about the new field, appended to upstream's tool description and system
# prompt rather than replacing them, so upstream's wording is not forked.
_SCOPE_GUIDANCE = ModelProse("""
## Declaring what a step will call

Every todo must carry a `tools` list naming the tools that step will call. A step that only reads,
reasons or reports declares an empty list. Name the tools exactly as they are advertised to you.

This list is what a human approves. Once a plan is approved, a tool no step declared is refused,
and adding it to the list afterwards does not change that — the approval carries the declaration
the person actually read. So if you find you need a tool the approved plan does not name, rewrite
the plan to include it and ask for the new plan to be approved.""")


def _write_scoped_todos(runtime: ToolRuntime[Any, Any], todos: list[ScopedTodo]) -> Command[Any]:
    """Replace the plan with `todos` — upstream's own effect, over the wider item.

    First-party rather than importing upstream's private `_write_todos`; the channel and tool names
    are pinned in `tests/test_upstream_surface.py`.
    """
    return Command(
        update={
            "todos": todos,
            "messages": [
                ToolMessage(f"Updated todo list to {todos}", tool_call_id=runtime.tool_call_id)
            ],
        }
    )


async def _awrite_scoped_todos(
    runtime: ToolRuntime[Any, Any], todos: list[ScopedTodo]
) -> Command[Any]:
    """The async arm of `_write_scoped_todos` — rewriting a plan awaits nothing."""
    return _write_scoped_todos(runtime, todos)


class ScopedTodoListMiddleware(TodoListMiddleware):
    """`TodoListMiddleware`, with every step declaring the tools it will call.

    Keeps upstream's prompt, parallel-rewrite guard, `todos` channel and `write_todos` name; only
    the
    argument schema is widened and the two texts gain a paragraph. Upstream's wording is read from
    the
    instance's attributes (pinned in `tests/test_upstream_surface.py`).
    """

    def __init__(self) -> None:
        """Take upstream's prompts and description, extend both, and rebind the tool."""
        super().__init__()
        self.system_prompt = f"{self.system_prompt}\n{_SCOPE_GUIDANCE}"
        self.tool_description = f"{self.tool_description}\n{_SCOPE_GUIDANCE}"
        self.tools = [
            StructuredTool.from_function(
                name="write_todos",
                description=self.tool_description,
                func=_write_scoped_todos,
                coroutine=_awrite_scoped_todos,
                args_schema=ScopedWriteTodosInput,
                infer_schema=False,
            )
        ]


def step_declaration(todo: Mapping[str, Any]) -> list[str]:
    """One step's declaration as the scope reader sees it: sorted, deduplicated, strings only.

    The single reading under both `declared_scope` and `plan_gate.plan_identity`, so the scope and
    the identity cannot disagree. Unreadable entries contribute nothing (fail closed for the scope).
    Sorted and deduplicated so a reordering does not revoke a live approval.
    """
    declared = todo.get(TOOLS_FIELD)
    if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
        return []
    return sorted({name for name in declared if isinstance(name, str)})


def declared_scope(todos: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Every tool this plan's steps declare — the scope a human approving it would authorize.

    The union over steps, because the gate judges a call against the plan: a batch commonly ticks
    step
    N while running step N+1's tool.
    """
    return frozenset(name for todo in todos for name in step_declaration(todo))
