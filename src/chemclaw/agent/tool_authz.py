"""Per-tool authorization as tool-call middleware (plan Phase F10-C).

Where `chemclaw.agent.audit` records every tool call, this **gates** every tool call: before a tool
runs, `enforce_tool_authz` asks `chemclaw.agent.authz.authorize_tool` whether the turn's user may
invoke it, and lets `AuthorizationError` propagate to block the call. It generalizes the single
expensive-trigger gate (F4-T5) so per-tool RBAC is applied uniformly by one interceptor, not
hand-wired into each tool — the same DRY move the audit trail makes.

The decisions live in `chemclaw.agent.authz` (the one home for authorization) and in the four
framework-free functions below; the `wrap_tool_call` wrappers at the end are only the wiring,
exactly as `chemclaw.agent.audit` is the wiring over the audit decision. They are safe to attach
unconditionally: `authorize_tool` is a no-op unless `entra_required`, so the dev path is unaffected
(the gate is open with no tenant). Attach them *inside* the audit middleware so a denied attempt is
still recorded as an `error` outcome before the exception surfaces.
"""

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

from chemclaw.agent.authz import AuthorizationError, authorize_tool, side_effecting_tools
from chemclaw.agent.turn_flags import is_dry_run
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError
from chemclaw.core.turn_signals import record_tool_failure

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


# --- the decisions, framework-free ---------------------------------------------------------------
#
# Each of the five gates below is one sentence of policy wrapped in the framework's plumbing. The
# sentence lives here, apart from that plumbing, because it was written while two engines had to
# agree on it: a dry-run refusal worded one way under one engine, or a denial the model is told
# about under one and not the other, was the drift the migration was arranged to be incapable of.
# One engine is left and the separation still earns its place — the plumbing is the part that
# changes when a library does, and the policy is the part that must not.


def dry_run_refusal(name: str) -> DryRunRefusal | None:
    """The refusal a side-effecting tool earns on a dry-run turn, or `None` to let it through."""
    if is_dry_run() and name in side_effecting_tools():
        return DryRunRefusal(
            f"DRY RUN — {name} changes stored data or starts work, so it was not called. "
            "Nothing was started; re-ask without dry-run to do it."
        )
    return None


def denial_result(exc: AuthorizationError) -> str:
    """What the model is told when a call was refused — the message verbatim, never swallowed."""
    return f"Refused: {exc}"


def domain_error_result(exc: BaseException) -> str:
    """What the model is told when a tool raised one of the two deliberately-safe error types."""
    return f"Error: {exc}"


def failure_detail(exc: BaseException) -> str:
    """What the *chemist's* transcript is told a tool raised, bounded so it cannot flood."""
    return f"{type(exc).__name__}: {exc}"[:_FAILURE_CHARS]


# --- the wiring ----------------------------------------------------------------------------------
#
# Five `wrap_tool_call` wrappers over the four decisions above. A gate stops a call by returning a
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


@wrap_tool_call
async def refuse_writes_on_dry_run(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse any side-effecting tool while the turn is a dry run (`refuse_writes_on_dry_run`)."""
    refusal = dry_run_refusal(request.tool_call["name"])
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
    """Hand a bad-input or outage error to the model in its own words (`surface_domain_errors`)."""
    try:
        return await handler(request)
    except (ChemclawError, SubsystemUnavailableError) as exc:
        return _refusal_message(request, domain_error_result(exc))


@wrap_tool_call
async def announce_tool_failures(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Tell the chemist's stream a tool raised, then let it continue (`announce_tool_failures`).

    Innermost, closest to the tool body, so it sees the raw exception before either converter turns
    it into a result — whether the *model* got a readable explanation is a separate question from
    whether the step worked, and the transcript answers the second.
    """
    try:
        return await handler(request)
    except Exception as exc:
        record_tool_failure(request.tool_call["name"], failure_detail(exc))
        raise
