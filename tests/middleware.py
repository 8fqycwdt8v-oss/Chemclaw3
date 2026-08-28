"""Drive one `wrap_tool_call` middleware directly, without compiling a graph around it.

`create_agent` composes the chain inside its tool node, and `agent/tool_invocation.py` folds it for
a caller with no graph. A *test* of one middleware wants neither: it wants that middleware, one
call, and a handler it controls, so a failure names the decision rather than the composition.

The halves these replaced were plain async functions, so a test called them directly:
`await enforce_tool_authz(ctx, call_next)`. A `@wrap_tool_call` middleware is an `AgentMiddleware`
*instance* with an `awrap_tool_call(request, handler)` method, so the same test needs three lines
of adapter. It lives here rather than in each file for the reason `tests/fakes_langgraph.py`
exists: a double five modules need is a double that must have one definition.

Deliberately *one* helper. This module briefly also carried a `run_chain` that reimplemented
upstream's composition so a test could drive several middlewares at once — with no caller, and
duplicating the very thing it claimed to model. A test that wants the real composition should
compile a real graph (`tests/test_connector_safety_rubric.py` does), because a hand-rolled stand-in
can only ever prove the stand-in is consistent with itself.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain.agents.middleware.types import ToolCallRequest


def tool_request(
    name: str,
    args: dict[str, Any] | None = None,
    call_id: str = "call-1",
    tool: Any = None,
) -> Any:
    """A `ToolCallRequest` carrying only what a middleware reads.

    `state={}` and `runtime=None` are what LangChain documents for a request built outside a graph.
    `tool` defaults to `None` for the same documented reason and because it is also what `ToolNode`
    passes for a name the graph does not hold — the governance chain reads `request.tool_call` and,
    in two observational places, `request.tool`. Pass a tool where its *metadata* is the thing under
    test (`agent/audit.py::_served_by`) or where the *label* is (`agent/audit.py::metric_tool_name`,
    which reads `.name`); leaving it `None` is the honest default, not a convenience, since a
    middleware that needed it would be one that fails open without it.

    Both readers fail **closed** on `None` — `_served_by` yields `""` and `metric_tool_name` yields
    `"unknown"` — which is why `None` is also the right fixture for the case they exist to handle:
    `ToolNode` passes it for a name the graph does not hold, i.e. a name the model invented.
    """
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id, "type": "tool_call"},
        tool=tool,
        state={},
        runtime=cast(Any, None),
    )


async def run_middleware(
    middleware: Any, request: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Any:
    """Call `middleware` around `handler` for `request`, and return what came back."""
    return await middleware.awrap_tool_call(request, handler)
