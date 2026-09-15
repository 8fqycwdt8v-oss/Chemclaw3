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
`plan_approvals`, never off the live plan. That direction is what stops a rewrite widening an
approval that has already been given.

**It is not what stops one widening an approval that is still being given, and that took a second
decision** (`D-2026-09-13-a-plan-identity-that-omits-the-scope-approves-a-plan-nobody-read`). This
paragraph used to end by arguing that hashing `content` only was safe *because* the stamped scope is
the one the human saw. Both clauses were true and the conclusion was false, because the scope is
stamped by reading the **live** plan at decide time: shown a plan declaring nothing, a model that
keeps every step's text and widens its `tools` leaves the identity unchanged, so the chemist's own
hash still satisfies the route's 409 freshness guard and the widened declaration is what gets
recorded. Driven end to end through `POST /sessions/{id}/plan/decision`: shown scope `[]`, rewritten
scope `['record_knowledge_note', 'watch_for']`, hash unchanged, 204, both tools then ran.
`plan_identity` now hashes each step's `content` *and* its declaration, by way of
`step_declaration` below — so the identity a human decides on is the whole of what they are
deciding, and a widening rewrite is a different plan that has to be shown and approved again. A
status flip still hashes identically, which is the property the content-only rule existed for.
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

        Both halves were unbounded and each sizes something that outlives the call — the step count
        sizes the durable approval row, the per-step declaration sizes the union in
        `plan_approvals.scope` and the refusal sentence built from it (measured at 600,192
        characters over 50,000 ten-character names). See `core/config/agent.py`, which carries the
        arithmetic and why this is a bound rather than a gate.

        The message names the step and the count and says what to do, because this is refused at
        argument validation precisely so the model can read it and split the plan.

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


def step_declaration(todo: Mapping[str, Any]) -> list[str]:
    """One step's declaration as the scope reader sees it: sorted, deduplicated, strings only.

    The primitive under both readings of a declaration — the scope a decision records
    (`declared_scope`) and the identity a decision is keyed on (`plan_gate.plan_identity`) — and it
    is one function because the two must not be able to disagree. **An identity derived from a
    *different* reading of `tools` than the scope is derived from is the defect this module's header
    records**, one layer down: if this narrowed a value that the hash kept whole, or kept one the
    hash narrowed, there would again be a pair of plans that authorize differently and hash alike.

    Unreadable entries contribute nothing — a non-list `tools`, a non-string element. That is the
    fail-closed direction for the scope, and for the identity it is merely conservative: a
    declaration this cannot read narrows the authorization, and a change to the unreadable part
    leaves the hash alone, which costs at most a re-approval nobody needed.

    Sorted and deduplicated because neither the order of a step's declaration nor a repeat in it
    changes what the step may call, and an identity that moved on a reordering would revoke a live
    approval for no reason a chemist could see.
    """
    declared = todo.get(TOOLS_FIELD)
    if not isinstance(declared, Sequence) or isinstance(declared, str | bytes):
        return []
    return sorted({name for name in declared if isinstance(name, str)})


def declared_scope(todos: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Every tool this plan's steps declare — the scope a human approving it would authorize.

    The union over steps rather than a per-step check, because the gate judges a call against the
    *plan*, not against whichever step happens to be in progress: a batch that ticks step N while
    running step N+1's tool is the canonical harness shape (`plan_gate.plan_after_batch`), and a
    per-step scope would refuse exactly it.

    Per-step reading is `step_declaration`'s, so the scope and the plan identity narrow a malformed
    declaration the same way.
    """
    return frozenset(name for todo in todos for name in step_declaration(todo))
