"""Run one tool through the governed chain when no graph is driving (M13 Step 6).

A chat turn's tool call is wrapped by six `wrap_tool_call` middlewares — two error converters,
the audit trail, the authorization gate, the dry-run and repeat guards, and the failure announcer
— and LangChain composes them inside `create_agent`'s tool node. A Temporal activity replaying a
template's `tool` step has the same obligation and no tool node: the whole point of
`durable/template_activities.py` is that a template's calls are governed *identically* to a
conversation's, because a template naming a role-gated tool must not run it for anyone who can run
the template (D-168).

**One composition, exposed rather than re-derived.** The chain's order is load-bearing and argued
for in `agent/langgraph_agent.tool_governance_middleware`; this module calls that function rather
than listing the middlewares again. A second list would be a second answer to "what governs a tool
call", and the first time the order changed the two would disagree silently — the exact shape of
the defect D-168 fixed, where the template path hand-applied two of the six and reached the
connector directly for the rest.

**What it does *not* take is the two model-facing converters**, and that is the one deliberate
difference from a chat turn. They translate an exception into prose a model can act on; a template
step has no model, and its result is interpolated into later steps. Converting a refusal there made
a refused `job` step return the refusal as its payload and launch the workflow anyway.

**What replaced the MAF version, and what stopped being needed.** This used to build a
`FunctionInvocationContext` by hand and drive `audit(context, lambda: enforce_tool_authz(...))` —
two of the six, hand-nested, with the other four absent. Two workarounds went with it:

- `skip_parsing=True`, which existed because MAF's `invoke` re-wrapped every result in
  `list[Content]` and Temporal's data converter refuses that type outright ("Unable to serialize
  unknown type: agent_framework._types.Content"), so a `tool` step could never return at all. A
  LangChain tool returns its own value.
- most of `_serializable`, which unwrapped that envelope. What survives of it is the MCP case,
  which is not a framework artifact: an MCP tool answers as content blocks on the wire whatever
  calls it.
"""

from collections.abc import Callable
from typing import Any, cast

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from chemclaw.agent.audit import AuditSink, make_audit_middleware
from chemclaw.agent.profiles import AgentProfile
from chemclaw.core.ids import stable_hash


def _request(tool: BaseTool, arguments: dict[str, Any]) -> ToolCallRequest:
    """The call as the middlewares expect to read it.

    `runtime=None` is what LangChain documents for a request built outside a graph, and it is the
    reason this seam is possible at all rather than a shim: the middlewares here read only
    `request.tool_call`, so none of them needs a runtime. `state` is empty for the same reason —
    a template step has no conversation state, and nothing in the chain looks for any.

    The call id is derived from the tool and its arguments rather than random, so a retried
    activity produces the same id: Temporal retries a step, and an audit trail in which one
    logical call appears under three ids reads as three calls.
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
) -> Any:
    """Call `tool` through the same chain a chat turn applies, and return what it produced.

    Args:
        tool: The tool to run, already found on the assembled surface by the caller.
        arguments: The step's arguments, as the template declared them.
        correlation_id: The run's correlation id, so the audit row joins to the rest of the run.
        actor: The run's actor. The audit trail attributes to a person, never to "the template".
        profile: The step's profile, which decides whether the plan gate is in the chain — the
            same question `build_langgraph_agent` asks, asked the same way.
        sink: The audit sink; `None` takes the configured default.

    Returns:
        Whatever the tool returned.

    Raises:
        AuthorizationError, PlanNotApprovedError, DryRunRefusal, ChemclawError: whatever the chain
            or the tool raised, **unconverted**. That is the difference between this and a chat
            turn, and it is deliberate: `surface_authorization_denials` and
            `surface_domain_errors` exist to hand a *model* something readable instead of an
            exception, and a template step has no model. Converting here made a refused `job` step
            return the refusal as its resolved payload and launch the workflow anyway, and a
            refused `tool` step interpolate "you are not authorized" into a later step as though it
            were an answer.
    """
    # Imported here rather than at module scope: `langgraph_agent` reaches the connector registry
    # and the whole tool surface, and this module is imported by a Temporal worker that must not
    # pay that at import time. It is the same deferral `template_activities._agent_surface` makes.
    from chemclaw.agent.langgraph_agent import tool_governance_middleware

    audit = make_audit_middleware(correlation_id=correlation_id, actor=actor, sink=sink)

    async def _call(request: ToolCallRequest) -> Any:
        """The innermost handler: the tool body itself."""
        return await request.tool.ainvoke(request.tool_call["args"])  # type: ignore[union-attr]

    handler: Callable[[ToolCallRequest], Any] = _call
    # Folded in reverse so the *first* entry ends up outermost, which is how LangChain composes the
    # same list inside `create_agent`. Reproducing the nesting direction matters more than it looks:
    # audit sits outside authorization precisely so a denied attempt is still a recorded attempt,
    # and folding the other way would record only the calls that were allowed to run.
    for middleware in reversed(tool_governance_middleware(audit, profile)):
        handler = _wrapped(middleware, handler)

    result = await handler(_request(tool, arguments))
    # A `ToolMessage` can still arrive from a middleware this chain *does* include if a future one
    # short-circuits, so it is unwrapped rather than returned as an envelope no caller expects.
    return result.content if isinstance(result, ToolMessage) else result


def _wrapped(
    middleware: Any, handler: Callable[[ToolCallRequest], Any]
) -> Callable[[ToolCallRequest], Any]:
    """Bind one middleware around `handler`, as its own closure over both.

    A named function rather than a lambda in the loop above, because a lambda would close over the
    loop variables by reference and every layer would end up calling the last middleware — the
    classic late-binding bug, and one that would produce a chain that still *runs* and governs
    nothing.
    """

    async def _layer(request: ToolCallRequest) -> Any:
        return await middleware.awrap_tool_call(request, handler)

    return _layer
