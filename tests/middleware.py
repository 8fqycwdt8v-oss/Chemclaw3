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


def tool_request(name: str, args: dict[str, Any] | None = None, call_id: str = "call-1") -> Any:
    """A `ToolCallRequest` carrying only what a middleware reads.

    `tool=None`, `state={}` and `runtime=None` are what LangChain documents for a request built
    outside a graph — the middlewares under test read `request.tool_call` and nothing else, which
    is the property that makes them testable this way at all rather than only through a turn.
    """
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {}, "id": call_id, "type": "tool_call"},
        tool=None,
        state={},
        runtime=cast(Any, None),
    )


async def run_middleware(
    middleware: Any, request: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Any:
    """Call `middleware` around `handler` for `request`, and return what came back."""
    return await middleware.awrap_tool_call(request, handler)
