"""Run one tool through the governed chain when no graph is driving.

A Temporal activity replaying a template's `tool` step must govern the call exactly as a
conversation does (audit, authorization, dry-run, repeat guard, plan gate, failure announcer), so a
template cannot run a role-gated tool for someone who may only run the template. This module
composes `agent/langgraph_agent.tool_governance_middleware` rather than listing the middlewares
again; `tests/test_middleware_order.py::_EXPECTED_ORDER` states the sequence.

It deliberately omits the two model-facing error converters and the framing wrapper: they serve a
model, and a template step has none. Converting a refusal would hand it to later steps as a payload
(and let a refused `job` step launch anyway), and framing would interpolate delimiters into later
arguments.
"""

from collections.abc import Callable
from typing import Any, cast

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException

from chemclaw.agent.audit import AuditSink, make_audit_middleware, returned_failure
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.tool_authz import returned_failure_detail
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash


class ToolReturnedFailure(ChemclawError):
    """A tool answered with a failure instead of a result, on a path that has no model to tell.

    A `ChemclawError`, so `durable/publish.py` treats it as non-retryable (the server answered); its
    own class so a tool's verdict is distinguishable from the activity itself breaking.
    """


def _request(tool: BaseTool, arguments: dict[str, Any]) -> ToolCallRequest:
    """The call as the middlewares expect to read it.

    `runtime=None` and empty `state` are what LangChain documents for a request built outside a
    graph; the middlewares read only `request.tool_call`. The call id is derived from the tool and
    arguments, so a retried activity keeps one id in the audit trail.
    """
    call_id = f"tmpl-{stable_hash({'tool': tool.name, 'arguments': arguments})[:16]}"
    return ToolCallRequest(
        tool_call={"name": tool.name, "args": arguments, "id": call_id, "type": "tool_call"},
        tool=tool,
        state={},
        runtime=cast(Any, None),
    )


async def invoke_governed(
    tool: BaseTool,
    arguments: dict[str, Any],
    *,
    correlation_id: str,
    actor: str,
    profile: AgentProfile,
    sink: AuditSink | None = None,
    want_message: bool = False,
) -> Any:
    """Call `tool` through the same chain a chat turn applies, and return what it produced.

    Args:
        tool: The tool to run, already found on the assembled surface by the caller.
        arguments: The step's arguments, as the template declared them.
        correlation_id: The run's correlation id, so the audit row joins to the rest of the run.
        actor: The run's actor. The audit trail attributes to a person, never to "the template".
        profile: The step's profile, which decides whether the plan gate is in the chain, as in
        `build_langgraph_agent`.
        sink: The audit sink; `None` takes the configured default.
        want_message: Return the whole `ToolMessage` instead of its content, so a caller can reach
        `.artifact`. Off by default because a `ToolMessage` coerces a dict return to text, which the
        `job` step cannot use (`test_template_job_step.py`); only the `tool` step opts in.

    Returns:
        Whatever the tool returned, or the `ToolMessage` carrying it when `want_message`.

    Raises:
        AuthorizationError, PlanNotApprovedError, DryRunRefusal, ChemclawError: whatever the chain
        or the tool raised, **unconverted**: a template step has no model to read a converted
        refusal, and must fail rather than pass the refusal on as data.
    """
    # Deferred import: `langgraph_agent` pulls in the connector registry and tool surface, which a
    # Temporal worker must not pay at import time.
    from chemclaw.agent.langgraph_agent import tool_governance_middleware

    audit = make_audit_middleware(correlation_id=correlation_id, actor=actor, sink=sink)

    async def _call(request: ToolCallRequest) -> Any:
        """The innermost handler: the tool body itself, with its error handler off.

        `langchain_mcp_adapters` gives connector tools a `handle_tool_error` callback that turns a
        server failure into an ordinary return value, so invoked with bare arguments a failed call
        is indistinguishable from an answer: the audit trail would record it as `ok` and later steps
        would consume the error text as data. Invoking with the whole call instead would coerce dict
        returns to text, breaking the `job` step. That callback exists to keep a model in the loop,
        and a template step has none, so it is disabled on a copy (the chat turn's tool object is
        untouched) and the failure raises.
        """
        tool = cast(Any, request.tool).model_copy(update={"handle_tool_error": False})
        if want_message:
            return await tool.ainvoke(request.tool_call)
        return await tool.ainvoke(request.tool_call["args"])

    handler: Callable[[ToolCallRequest], Any] = _call
    # Folded in reverse so the first entry is outermost, as `create_agent` composes it; audit must
    # sit outside authorization so a denied attempt is still recorded.
    for middleware in reversed(tool_governance_middleware(audit, profile)):
        handler = _wrapped(middleware, handler)

    # A tool that reports failure by answering must still fail the step. `_call` disables the
    # adapter's error handler so the failure raises; it propagates through the chain, so audit and
    # the announcer record it once before it is converted here.
    try:
        result = await handler(_request(tool, arguments))
    except ToolException as exc:
        raise ToolReturnedFailure(str(exc)) from exc
    # A middleware this chain *does* include can still short-circuit with a `ToolMessage`, so the
    # same question is asked of a returned one before it is unwrapped.
    if (failed := returned_failure(result)) is not None:
        raise ToolReturnedFailure(returned_failure_detail(failed))
    if want_message:
        return result
    return result.content if isinstance(result, ToolMessage) else result


def _wrapped(
    middleware: Any, handler: Callable[[ToolCallRequest], Any]
) -> Callable[[ToolCallRequest], Any]:
    """Bind one middleware around `handler`, as its own closure over both.

    A named function rather than a lambda in the loop, which would late-bind the loop variables and
    silently govern nothing.
    """

    async def _layer(request: ToolCallRequest) -> Any:
        return await middleware.awrap_tool_call(request, handler)

    return _layer
