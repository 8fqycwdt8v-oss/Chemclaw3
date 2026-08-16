"""Per-tool authorization as tool-call middleware (plan Phase F10-C).

Where `chemclaw.agent.audit` records every tool call, this **gates** every tool call: before a tool
runs, `enforce_tool_authz` asks `chemclaw.agent.authz.authorize_tool` whether the turn's user may
invoke it, and lets `AuthorizationError` propagate to block the call. It generalizes the single
expensive-trigger gate (F4-T5) so per-tool RBAC is applied uniformly by one interceptor, not
hand-wired into each tool — the same DRY move the audit trail makes.

The decisions live in `chemclaw.agent.authz` (the one home for authorization) and in the
framework-free functions below; the `wrap_tool_call` wrappers at the end are only the wiring,
exactly as `chemclaw.agent.audit` is the wiring over the audit decision. They are safe to attach
unconditionally: `authorize_tool` is a no-op unless `entra_required`, so the dev path is unaffected
(the gate is open with no tenant). Attach them *inside* the audit middleware so a denied attempt is
still recorded as an `error` outcome before the exception surfaces.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.audit import returned_failure
from chemclaw.agent.authz import (
    AuthorizationError,
    authorize_tool,
    side_effecting_call,
    side_effecting_tools,
)
from chemclaw.agent.turn_flags import is_dry_run
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.turn_signals import record_tool_failure

logger = logging.getLogger(__name__)

# How much of a failure message reaches the trace. Long enough for a chemist to recognise the
# problem, short enough that an unexpected exception's text cannot flood the stream.
_FAILURE_CHARS = 300


class DryRunRefusal(AuthorizationError):
    """A side-effecting tool was called on a turn the caller marked `dry_run`.

    An `AuthorizationError` subclass for the reason `PlanNotApprovedError` is one: the two
    behaviours already built around that class are the two wanted here — the audit middleware
    records the refusal, and `surface_authorization_denials` hands the model the message verbatim
    rather than a generic tool failure. A subclass rather than the base so a caller can
    tell "you lack a role" apart from "you asked me not to actually do this", which are different
    situations with different next steps.
    """


class UndeclaredWriteRefusal(AuthorizationError):
    """A side-effecting tool was called by a narrowed agent that was never given it.

    Raised where the *structure* already answers — the tool is not bound to the graph, so it cannot
    run whatever this says. That is exactly why it exists: **this enforces nothing and it changes
    what everyone reads.** Measured by removing it and running the same turn:

        without:  ToolMessage(status="error") "Error: compute_xtb_energy is not a valid tool, try
                  one of [list_attachments, read_attachment, ask_clarifying_question, …]"
                  → audit `detail` is that same sentence
        with:     "Refused: compute_xtb_energy changes stored data or starts work, and this agent
                  was not given it, so it was not called…"

    Three things are wrong with the first. `status="error"` reaches Anthropic as `is_error` on the
    tool_result block — the retry-inviting signal `_refusal_message` exists to avoid. The text
    invites the retry in words too ("try one of"), for a tool that was withheld on purpose rather
    than mistyped. And it enumerates the agent's whole remaining inventory into the transcript and
    into the audit trail, where the `detail` column is what a reviewer reads as *what happened*.

    The audit row itself is not what this buys, and the earlier draft of this docstring said it was.
    `ToolNode` *returns* the invalid-name message rather than raising it, and it returns it from
    inside the wrapper chain — so `returned_failure` already books an `outcome="error"` row either
    way. What changes is what that row says.

    An `AuthorizationError` subclass for the reason `DryRunRefusal` and `PlanNotApprovedError` are:
    the audit middleware records it and `surface_authorization_denials` relays it verbatim rather
    than as a fault. Its own name so a reader can tell "this agent was never given that tool" apart
    from "your account may not use it".
    """


# --- the decisions, framework-free ---------------------------------------------------------------
#
# Each of the decisions below is one sentence of policy wrapped in the framework's plumbing. The
# sentence lives here, apart from that plumbing, because it was written while two engines had to
# agree on it: a dry-run refusal worded one way under one engine, or a denial the model is told
# about under one and not the other, was the drift the migration was arranged to be incapable of.
# One engine is left and the separation still earns its place — the plumbing is the part that
# changes when a library does, and the policy is the part that must not.


def dry_run_refusal(name: str, arguments: Mapping[str, Any]) -> DryRunRefusal | None:
    """The refusal a side-effecting call earns on a dry-run turn, or `None` to let it through.

    Takes the arguments as well as the name because one of the calls it must refuse cannot be
    recognised from the name — `write_file` under `/memories/` is durable and the same verb under
    `/scratch/` is not. See `authz.side_effecting_call`.
    """
    if is_dry_run() and side_effecting_call(name, arguments):
        return DryRunRefusal(
            f"DRY RUN — {name} changes stored data or starts work, so it was not called. "
            "Nothing was started; re-ask without dry-run to do it."
        )
    return None


def undeclared_write_refusal(name: str, held: frozenset[str]) -> UndeclaredWriteRefusal | None:
    """The refusal a write earns from an agent narrowed away from it, or `None` to let it through.

    `held` is the profile's resolved `tool_names` — what this agent was actually built with. A name
    outside it that also changes something is the case worth wording; a name outside it that changes
    nothing is an ordinary hallucinated or stale tool name, and inventing an authorization sentence
    for that would tell a model it was *refused* something that simply does not exist here.
    """
    if name in held or name not in side_effecting_tools():
        return None
    return UndeclaredWriteRefusal(
        f"{name} changes stored data or starts work, and this agent was not given it, so it was "
        "not called. Nothing was started; say what you could not do and continue with what you can."
    )


def denial_result(exc: AuthorizationError) -> str:
    """What the model is told when a call was refused — the message verbatim, never swallowed."""
    return f"Refused: {exc}"


def domain_error_result(exc: BaseException) -> str:
    """What the model is told when a tool raised one of the two deliberately-safe error types."""
    return f"Error: {exc}"


def failure_detail(exc: BaseException) -> str:
    """What the *chemist's* transcript is told a tool raised, bounded so it cannot flood."""
    return f"{type(exc).__name__}: {exc}"[:_FAILURE_CHARS]


def returned_failure_detail(message: ToolMessage) -> str:
    """The same sentence for a tool that *returned* its failure instead of raising one.

    Beside `failure_detail` rather than folded into it because the two have nothing to render in
    common: a raised failure has an exception class worth naming, while a returned one has only the
    server's own words — an MCP tool's error content is already a sentence someone wrote for a
    reader, and prefixing it with a type name would be inventing a classification nobody made.

    `message.text` rather than `message.content`: MCP content arrives as a list of content blocks,
    so a chemist reading `content` would get `[{'type': 'text', 'text': …}]` — a repr of the
    transport where the explanation should be. Bounded by the same `_FAILURE_CHARS` for the same
    reason: a remote error is exactly the text that can be arbitrarily long.
    """
    return message.text[:_FAILURE_CHARS]


def answered_failure(message: ToolMessage) -> ToolMessage:
    """The same returned failure, minus the flag a provider reads as "retry this".

    **The policy `_refusal_message` states held on two of the three tool kinds and was inverted on
    the third.** An in-process tool and a job tool both fail by *raising*, so both converters answer
    the model with `_refusal_message` — which is deliberately not `status="error"`, because that
    reaches Anthropic as `is_error` on the tool_result block and invites exactly the retry a
    deliberately-worded refusal exists to prevent. An MCP tool never raises: the adapter converts an
    `isError=True` result inside `StructuredTool.ainvoke` and *returns*
    `ToolMessage(status="error")` (see `agent/audit.returned_failure`). So the one kind that carries
    most domain refusals — a connector is where a bad SMILES, a molecule outside a model's domain or
    an offline instrument is diagnosed — was the one kind sending the retry flag.

    The words are kept verbatim rather than re-worded. They are the server's own sentence about what
    went wrong, already narrowed to a caller-safe family by `connectors/server.py`'s tool-error
    sanitizer, and replacing them with a classification nobody made is the mistake
    `returned_failure_detail` refuses for the same text on the way to the transcript.

    **Only what the model reads changes, and that is the whole reason this sits where it does.**
    Every reader that records the call as a failure is *inside* this converter and has already run
    by the time it returns: the audit trail books `outcome="error"` (`agent/audit.py`) and the
    chemist's transcript gets its failure signal (`announce_tool_failures`). Clearing the flag any
    lower — at the MCP seam, where `langchain_mcp_adapters` offers a `ToolCallInterceptor` that
    could rewrite the `CallToolResult` before it is ever converted — would hide the failure from
    both of them and re-open the defect they were built to close. A refusal is an answer to the
    model and still a failure in the record.

    One reader downstream does change, and it is named here rather than left to be discovered:
    `api/graph_stream.py` decides "this call is not a result, so it must not become evidence" from
    the same `status` field, so a connector failure now reaches the trace the way an in-process
    domain refusal already does — as a `tool_result` beside the `tool_failed`. That is the
    inconsistency being removed rather than a new one: the exclusion never applied to the two kinds
    that raise, because both of their converters answer with a `_refusal_message` whose status is
    `"success"`. A status-independent test of "did this call fail" belongs in that module, which
    already sees the turn's failure signals.
    """
    return message.model_copy(update={"status": "success"})


def unexpected_error_result() -> str:
    """What the model is told when a tool raised something outside the two safe families.

    Deliberately says nothing about the exception. The two safe families are safe *because* someone
    decided their messages are fit for a model to read; anything else is an internal fault whose
    text can carry a DSN, a path or a row of data, which is why `include_detailed_errors` is off by
    default. The chemist's transcript still gets the type and message through
    `announce_tool_failures` — a person debugging their own deployment is a different audience from
    a model composing an answer.
    """
    return (
        "Error: that tool failed unexpectedly and returned nothing. Do not retry it with the same "
        "arguments; say what you were unable to do, and continue with what you can."
    )


# --- the wiring ----------------------------------------------------------------------------------
#
# The `wrap_tool_call` wrappers over the decisions above. A gate stops a call by returning a
# `ToolMessage` instead of calling `handler` — "the tool body never ran and this is what the model
# is told instead" — which is why `_refusal_message` is shared rather than written out five times.


def _refusal_message(request: Any, text: str) -> ToolMessage:
    """A tool result the model reads as this call's answer, carrying the id it must reply to.

    `tool_call_id` is not optional bookkeeping: an assistant `tool_use` block with no matching
    `tool_result` is a malformed exchange that the provider rejects outright, so a gate that
    refused without echoing the id would turn a refusal into a dead turn.

    **Deliberately not `status="error"`.** A refusal is the tool's *successful* result — its
    message becomes the answer to the call, verbatim — so the model reads it as what happened
    rather than as a transient failure worth retrying. `status="error"` reaches Anthropic as
    `is_error` on the tool_result block, which is the opposite signal: it invites exactly the retry
    a deliberately-worded refusal is trying to prevent.
    """
    return ToolMessage(content=text, tool_call_id=request.tool_call["id"])


@wrap_tool_call
async def enforce_tool_authz(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Block a tool call the turn's user is not authorized for, else run it unchanged."""
    authorize_tool(request.tool_call["name"])
    return await handler(request)


def refuse_undeclared_writes(held: frozenset[str]) -> Any:
    """Middleware wording an undeclared write's refusal for a profile narrowed to `held`.

    A factory rather than a module-level wrapper because the answer depends on *which* agent this
    is, and the four wrappers beside it read only ambient state. Attached by
    `langgraph_agent.tool_governance_middleware` only when a profile narrows at all, so an
    un-narrowed agent's chain is byte-identical to the one before this existed.

    **It intercepts a tool the graph does not hold, and that is not an accident of ordering.**
    `ToolNode` looks the name up with `tools_by_name.get(...)` and passes `tool=None` into the
    request — "validation is deferred to `_execute_tool_async` to allow interceptors to
    short-circuit requests for unregistered tools", in its own words — so a `wrap_tool_call`
    middleware still runs, and raising here means the name is never validated and the body never
    reached. Nothing in this chain reads `request.tool`.
    """

    @wrap_tool_call(name="refuse_undeclared_writes")
    async def _refuse(request: Any, handler: Callable[[Any], Any]) -> Any:
        """Refuse a side-effecting tool this profile was narrowed away from."""
        refusal = undeclared_write_refusal(request.tool_call["name"], held)
        if refusal is not None:
            raise refusal
        return await handler(request)

    # Named explicitly rather than after the closure, because `wrap_tool_call` takes the function's
    # name as the middleware class's — and a chain that reads `_refuse` in a trace or a repr is one
    # more indirection between a refusal and the rule that produced it.
    return _refuse


@wrap_tool_call
async def refuse_writes_on_dry_run(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse any side-effecting tool while the turn is a dry run (`refuse_writes_on_dry_run`)."""
    refusal = dry_run_refusal(request.tool_call["name"], request.tool_call.get("args") or {})
    if refusal is not None:
        raise refusal
    return await handler(request)


@wrap_tool_call
async def surface_authorization_denials(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Hand a denial to the model verbatim (`surface_authorization_denials`).

    LangChain does not collapse a tool exception into an opaque "Function failed." the way the
    framework this replaced did, so what this buys is no longer message *recovery*: it keeps a
    deliberately-worded refusal from being reported as a tool error the model might retry. Attached
    outside the audit middleware, so the denial still reaches the trail as an `error` outcome
    first.
    """
    try:
        return await handler(request)
    except AuthorizationError as exc:
        return _refusal_message(request, denial_result(exc))


@wrap_tool_call
async def surface_domain_errors(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Turn any tool exception into a result the model can read, rather than ending the turn.

    The two safe families keep their own words; **everything else is converted too**, and that is
    the half that was missing. `announce_tool_failures` records a failure and re-raises, neither
    converter caught anything outside those two families, and `ToolNode`'s default handler re-raises
    what it is given — so a `KeyError` from a parser, a `TimeoutError`, or a driver's
    `ConnectionError` escaped the graph and killed the whole turn. The chemist lost the answer, the
    tokens and every other tool the turn had already run, for one failed step.

    That is a regression rather than a choice: the framework this replaced collapsed *any* tool
    exception into a result, so a failed tool had always been a recoverable step. The model is told
    something deliberately contentless (`unexpected_error_result`) because an unclassified
    exception's text is not vetted for a model to read; the *transcript* still carries the type and
    message, which is the audience that wants it.

    `BaseException` is not caught: `CancelledError` is how a disconnect and the turn deadline
    arrive, and converting one into a tool result would swallow the cancellation.

    **And a tool can fail without raising at all**, which is why this converter also inspects what
    came back. A connector's failure arrives as a returned `ToolMessage(status="error")`, so
    catching exceptions converted nothing for exactly the tools that run out of process — they were
    the one kind still handing the provider the retry flag (`answered_failure`). Both ways to fail
    end in the same place for the same reason: this is the one converter that answers the model
    about a *failed tool call*, and which side of the call the failure was signalled on is a
    property of the tool's transport, not of what the model should read.
    """
    try:
        result = await handler(request)
    except (ChemclawError, SubsystemUnavailableError) as exc:
        return _refusal_message(request, domain_error_result(exc))
    except AuthorizationError:
        # Left for `surface_authorization_denials`, which sits outside this one and words a refusal
        # differently from a fault. Catching it here would make every denial read as a crash.
        raise
    except Exception:
        logger.exception("tool %s raised an unhandled error", request.tool_call["name"])
        return _refusal_message(request, unexpected_error_result())
    failed = returned_failure(result)
    return result if failed is None else answered_failure(failed)


@wrap_tool_call
async def announce_tool_failures(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Tell the chemist's stream a tool failed, then let it continue (`announce_tool_failures`).

    Outermost of the governance middleware and inside both converters, so it sees every failure —
    a tool body raising, *and* a refusal raised by a gate below it — before either converter turns
    one into a result. (It sat innermost once, on the reasoning that the tool body is what fails;
    measured, that is where a plan-gate refusal became invisible, because the gate raises before
    calling the handler it wraps. `agent/langgraph_agent.tool_governance_middleware` has the
    numbers.) Whether the *model* got a readable explanation is a separate question from
    whether the step worked, and the transcript answers the second.

    **Two ways to fail, and only one of them raises.** A connector tool reaches this through
    `langchain_mcp_adapters`, which handles the error itself and *returns* a
    `ToolMessage(status="error")` — so catching exceptions announced nothing for exactly the tools
    that run out of process. Worse than silence: `api/graph_stream` suppresses an error
    `ToolMessage` on the documented ground that it "is already reported as tool_failed", so a failed
    connector call left a `tool_call` event with no result and no failure beside it and vanished
    from the transcript entirely. Checking the returned message closes that, and cannot
    double-report — `returned_failure` is `None` for anything that signalled by raising.
    """
    try:
        result = await handler(request)
    except Exception as exc:
        record_tool_failure(request.tool_call["name"], failure_detail(exc))
        raise
    failed = returned_failure(result)
    if failed is not None:
        record_tool_failure(request.tool_call["name"], returned_failure_detail(failed))
    return result
