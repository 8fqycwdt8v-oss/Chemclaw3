"""Drive one `wrap_tool_call` middleware directly, without compiling a graph around it.

A test of one middleware wants that middleware, one call and a handler it controls, so a failure
names the decision rather than the composition. A test of the composition should compile a real
graph (as `tests/test_connector_safety_rubric.py` does).
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
    `tool` defaults to `None`, which is also what `ToolNode` passes for a name the graph does not
    hold; pass one where its metadata or name is under test (`agent/audit.py`). Both readers fail
    closed on `None`.
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
