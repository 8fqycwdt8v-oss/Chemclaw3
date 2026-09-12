"""What a plan step is allowed to call, declared by the step and approved with it.

`D-2026-09-12-an-approval-that-names-no-tool-authorizes-every-tool`. D-137 made the plan approval
durable, D-167 made it bind an *act* rather than latch onto a session — and neither bounded what
the act may be. `enforce_plan_approval` asked one question, "does an approval stand for this
plan?", so an approval recorded against a one-line, read-only plan authorized every name in
`authz.side_effecting_tools()`: every knowledge-graph write, every durable launcher, every enabled
bundle's state-changing surface. `tests/test_plan_scope.py` drives that and holds the figure, which
is why no number for it appears here.

**The data has to come from the model, because nothing else knows it.** A plan step is prose; which
tools carry it out is a modelling decision taken when the step is written. So the plan tool's own
schema carries it: `write_todos` takes `tools` beside `content` and `status`, and the human
approving the plan approves that declaration with it.

**The field is required, and that is the decision this module exists to take deliberately.** The
obvious alternative is an optional field with a fall-through — a step that declares nothing keeps
today's unbounded behaviour — and that is a control that reads as one and is not: it bounds
nothing until the model volunteers to be bounded, which is the failure this repository keeps
finding in its own perimeter. The other alternative, an optional field that refuses when absent,
makes the first omission look like an authorization decision to a chemist who has approved a plan
and watched it refuse its own steps. A **required** field makes the omission unrepresentable
instead: a `write_todos` call without it fails the tool's own argument validation, the model reads
the error and rewrites the call, and no plan without a declaration ever reaches a human. An empty
list stays expressible and means what it says — this step changes nothing.

**A declaration is not an authorization.** `declared_scope` is only ever read to *record* what a
human approved (`api/routes/plan.py`, `cli/chat.py`); the gate reads the recorded scope back off
`plan_approvals`, never off the live plan. That is what makes the model unable to widen its own
scope: `plan_identity` hashes `content` only — deliberately, so the canonical "tick the step, run
its tool" batch keeps its approval — so a rewrite that keeps every step's text and adds a tool to
it hashes to the same approved plan and gains nothing, because the stamped scope is the one the
human saw.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from langchain.agents.middleware import TodoListMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel
from typing_extensions import TypedDict

# The key a step's declaration is spelled with, in the tool schema, in the state channel and in the
# two readers below. One constant because a rename has to move all of them at once — and because
# `tests/test_plan_scope.py` asserts the schema requires it by this name.
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
    """The `write_todos` argument schema — upstream's, with each step declaring its tools."""

    todos: list[ScopedTodo]


# What the model is told about the new field, appended to upstream's own tool description and to
# its system prompt rather than replacing either: everything upstream says about when to plan and
# how to keep the list current is as true here as there, and a fork of that text is a paragraph
# that goes stale on the next bump with nothing to notice.
_SCOPE_GUIDANCE = """
## Declaring what a step will call

Every todo must carry a `tools` list naming the tools that step will call. A step that only reads,
reasons or reports declares an empty list. Name the tools exactly as they are advertised to you.

This list is what a human approves. Once a plan is approved, a tool no step declared is refused,
and adding it to the list afterwards does not change that — the approval carries the declaration
the person actually read. So if you find you need a tool the approved plan does not name, rewrite
the plan to include it and ask for the new plan to be approved."""


def _write_scoped_todos(runtime: ToolRuntime[Any, Any], todos: list[ScopedTodo]) -> Command[Any]:
    """Replace the plan with `todos` — upstream's own effect, over the wider item.

    First-party rather than a reach for `langchain.agents.middleware.todo._write_todos`: the body
    is one `Command`, and importing a private function to avoid writing it would be a coupling to
    something upstream never published, in exchange for nothing. The channel name and the tool name
    are the couplings that matter and both are pinned in `tests/test_upstream_surface.py`.
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

    Everything upstream does is kept: the system prompt it injects, the parallel-rewrite guard in
    `after_model`, the `todos` channel and the `write_todos` name. Only the tool's argument schema
    is widened, and the two texts the model reads gain a paragraph about the new field.

    **The instance's own attributes are read rather than the module's constants**, so upstream's
    wording arrives on every bump instead of being forked here. That those attributes exist is the
    coupling, and `tests/test_upstream_surface.py` pins it.
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


def declared_scope(todos: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Every tool this plan's steps declare — the scope a human approving it would authorize.

    The union over steps rather than a per-step check, because the gate judges a call against the
    *plan*, not against whichever step happens to be in progress: a batch that ticks step N while
    running step N+1's tool is the canonical harness shape (`plan_gate.plan_after_batch`), and a
    per-step scope would refuse exactly it.

    Unreadable entries contribute nothing — a non-list `tools`, a non-string element. That is the
    fail-closed direction: the scope is what a call is checked *against*, so anything this cannot
    read narrows the authorization instead of widening it.
    """
    names: set[str] = set()
    for todo in todos:
        declared = todo.get(TOOLS_FIELD)
        if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
            continue
        names.update(name for name in declared if isinstance(name, str))
    return frozenset(names)
