"""Drive one `wrap_tool_call` middleware directly, without compiling a graph around it.

`create_agent` composes the chain inside its tool node, and `agent/tool_invocation.py` folds it for
a caller with no graph. A *test* of one middleware wants neither: it wants that middleware, one
call, and a handler it controls, so a failure names the decision rather than the composition.

The halves these replaced were plain async functions, so a test called them directly:
`await enforce_tool_authz(ctx, call_next)`. A `@wrap_tool_call` middleware is an `AgentMiddleware`
*instance* with an `awrap_tool_call(request, handler)` method, so the same test needs three lines
of adapter. They live here rather than in each file for the reason `tests/fakes_langgraph.py`
exists: a double five modules need is a double that must have one definition.
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


async def run_chain(
    middlewares: list[Any], request: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Any:
    """Nest `middlewares` outermost-first around `handler` — the composition `create_agent` uses.

    For the tests that are about the *order* rather than about one decision: audit outside
    authorization is what makes a denied attempt a recorded attempt, and only a chain can show it.
    """
    composed = handler
    for middleware in reversed(middlewares):
        composed = _bind(middleware, composed)
    return await composed(request)


def _bind(
    middleware: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Callable[[Any], Awaitable[Any]]:
    """One layer, as its own closure — a lambda here would late-bind the loop variable."""

    async def _layer(request: Any) -> Any:
        return await middleware.awrap_tool_call(request, handler)

    return _layer
