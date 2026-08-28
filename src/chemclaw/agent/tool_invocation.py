"""Run one tool through the governed chain when no graph is driving (M13 Step 6).

A chat turn's tool call is wrapped by the `wrap_tool_call` middlewares — two error converters, the
untrusted-content framer, the audit trail, the authorization gate, the dry-run and repeat guards,
and the failure announcer — and LangChain composes them inside `create_agent`'s tool node. They are
named rather than counted here: the number was written as "seven" and went stale the first time one
was added, which is the shape `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` rejects.
`tests/test_middleware_order.py::_EXPECTED_ORDER` is the one place the sequence is stated.

A Temporal activity replaying a
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

**What it does *not* take is the two model-facing converters, nor the framing wrapper**, and that
is the deliberate difference from a chat turn — all three exist to serve a model, and a template
step has no model. The converters translate an exception into prose a model can act on; a template
step's result is interpolated into later steps instead, and converting a refusal there made a
refused `job` step return the refusal as its payload and launch the workflow anyway. Framing is
withheld for the same reason and one more: the envelope marks retrieved text as data *for a model*,
so wrapping a template step's result would interpolate a delimiter into a later step's arguments.

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
from langchain_core.tools import BaseTool, ToolException

from chemclaw.agent.audit import AuditSink, make_audit_middleware, returned_failure
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.tool_authz import returned_failure_detail
from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash


class ToolReturnedFailure(ChemclawError):
    """A tool answered with a failure instead of a result, on a path that has no model to tell.

    A `ChemclawError` so `durable/publish.py` classifies it non-retryable: a server that reported
    `isError=True` has answered, and asking again gets the same answer. Its own class rather than a
    bare `ChemclawError` so a template's failed step can be told from a step that failed because the
    activity itself broke — one is the tool's verdict, the other is ours.
    """


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
    want_message: bool = False,
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
        want_message: Return the whole `ToolMessage` instead of just its content, so a caller can
            reach `.artifact`. **Off by default, and the default is the load-bearing half.**
            LangChain coerces a `ToolMessage`'s content to text for a non-block return, so the
            `job` step — whose tool returns a dict — gets `'{"subject": "benzene"}'` and
            `ResolvedJob` rejects it; three tests in `test_template_job_step.py` pin that. Only the
            `tool` step opts in, because only it needs the structured payload
            (`template_activities._call_governed`), and an MCP tool's content is a *list of blocks*
            rather than a dict, so it survives the wrap intact.

    Returns:
        Whatever the tool returned, or the `ToolMessage` carrying it when `want_message`.

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
        """The innermost handler: the tool body itself, with its error handler off.

        **The whole call, not just its arguments, and the difference is not cosmetic.** LangChain
        decides what a failing tool *returns* from the form it was invoked with. Measured on a tool
        built the way `langchain_mcp_adapters` builds one (a `ToolException` subclass plus a
        `handle_tool_error` callback), against the same failure:

            ainvoke(tool_call["args"])  ->  str        status=None      'Error: …'
            ainvoke(tool_call)          ->  ToolMessage status='error'  'Error: …'

        This path used the first form, so a failed connector call arrived indistinguishable from a
        successful one — a bare string. Every reader that decides success by looking at the result
        was therefore reading a failure as an answer, and two did: `audit._recording` books
        `returned_failure(result)`, which is `isinstance`-based and saw nothing, so a refused tool
        was recorded in the trail as `ok`; and the string became `${steps.<id>.result}`, so a later
        step — a `job` step included — ran on "the instrument is offline" as though it were data.

        **Invoking with the whole call is not the fix**, and trying it is what showed why. That form
        makes LangChain wrap the return in a `ToolMessage`, whose content is coerced to text — so a
        `job` step, whose tool returns a dict, got `'{"subject": "benzene"}'` and `ResolvedJob`
        rejected it. Three tests in `test_template_job_step.py` say so.

        What this path wants is the opposite of what `handle_tool_error` is for. That callback
        exists to keep a *model* in the loop: it converts a failure into prose the model can
        self-correct against. A template step has no model — the same argument this module makes for
        withholding the two model-facing converters and the framer — so the failure should simply
        *raise*, and
        `invoke_governed` turns it into `ToolReturnedFailure` below. Disabling it on a copy leaves
        the caller's tool untouched, which matters because the same tool object is the one a chat
        turn uses, and there the handler is exactly right.
        """
        tool = cast(Any, request.tool).model_copy(update={"handle_tool_error": False})
        if want_message:
            return await tool.ainvoke(request.tool_call)
        return await tool.ainvoke(request.tool_call["args"])

    handler: Callable[[ToolCallRequest], Any] = _call
    # Folded in reverse so the *first* entry ends up outermost, which is how LangChain composes the
    # same list inside `create_agent`. Reproducing the nesting direction matters more than it looks:
    # audit sits outside authorization precisely so a denied attempt is still a recorded attempt,
    # and folding the other way would record only the calls that were allowed to run.
    for middleware in reversed(tool_governance_middleware(audit, profile)):
        handler = _wrapped(middleware, handler)

    # **A tool that reports failure by answering must still fail the step.** An MCP tool never
    # raises of its own accord: `langchain_mcp_adapters` gives every connector tool a
    # `handle_tool_error` callback, so a server reporting `isError=True` is converted inside
    # `ainvoke` and comes back as an ordinary value. Measured on a tool built the way the adapter
    # builds one, against the same failure:
    #
    #     ainvoke(tool_call["args"])  ->  str          status=None      'Error: …'
    #     ainvoke(tool_call)          ->  ToolMessage   status='error'  'Error: …'
    #
    # This path took the first form, so a refused call arrived as a bare string, indistinguishable
    # from an answer. Two readers believed it: `audit._recording` decides by `returned_failure`,
    # which is `isinstance`-based and saw nothing, so the trail recorded a refused tool as `ok`; and
    # the sentence became `${steps.<id>.result}`, so the next step — a `job` step included — ran on
    # "the instrument is offline" as though it were data.
    #
    # `_call` disables the handler so the failure raises instead, which is what a step with no model
    # wants. It propagates *through* the chain, so audit books its `error` row and the announcer
    # reports it before this converts it — nothing is recorded twice.
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

    A named function rather than a lambda in the loop above, because a lambda would close over the
    loop variables by reference and every layer would end up calling the last middleware — the
    classic late-binding bug, and one that would produce a chain that still *runs* and governs
    nothing.
    """

    async def _layer(request: ToolCallRequest) -> Any:
        return await middleware.awrap_tool_call(request, handler)

    return _layer
