"""Per-tool authorization as one MAF function middleware (plan Phase F10-C).

Where `chemclaw.agent.audit` records every tool call, this **gates** every tool call: before a tool
runs,
`enforce_tool_authz` asks `chemclaw.agent.authz.authorize_tool` whether the turn's user may invoke
it, and
lets `AuthorizationError` propagate to block the call. It generalizes the single expensive-trigger
gate (F4-T5) so per-tool RBAC is applied uniformly by one interceptor, not hand-wired into each
tool — the same DRY move the audit trail makes.

The decision lives in `chemclaw.agent.authz` (the one home for authorization); this module is only
the MAF
wiring, exactly as `chemclaw.agent.audit` is the wiring over the audit decision. It is safe to
attach
unconditionally: `authorize_tool` is a no-op unless `entra_required`, so the classic/dev path is
unaffected (the gate is open with no tenant). Attach it *after* the audit middleware so a denied
attempt is still recorded as an `error` outcome before the exception surfaces.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import FunctionInvocationContext, function_middleware
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
    rather than MAF's opaque "Function failed." A subclass rather than the base so a caller can
    tell "you lack a role" apart from "you asked me not to actually do this", which are different
    situations with different next steps.
    """


# --- the decisions, framework-free ---------------------------------------------------------------
#
# Each of the five gates below is one sentence of policy wrapped in one framework's plumbing. The
# sentence lives here so that porting the plumbing to a second engine cannot reword it: a dry-run
# refusal that says something different depending on `agent_engine`, or a denial the model is told
# about under one engine and not the other, would be exactly the drift this migration is supposed
# to be incapable of. The engines below are wrappers over these four functions and nothing else.


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


# --- the MAF wiring ------------------------------------------------------------------------------


@function_middleware
async def enforce_tool_authz(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Block a tool call the turn's user is not authorized for, else run it unchanged.

    Raises:
        AuthorizationError: When `authorize_tool` denies the current user this tool. The tool body
            never runs; an outer audit middleware records the denied attempt as an error outcome.
    """
    authorize_tool(context.function.name)
    await call_next()


@function_middleware
async def refuse_writes_on_dry_run(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Refuse any side-effecting tool while the turn is a dry run.

    `dry_run: true` promised "show me what you would do without doing it", and it was checked in
    exactly three tools — the report launcher, the generated job launchers and the template
    launcher. Every other write was untouched, so a dry-run turn still pushed a branch to the
    knowledge repo (`propose_knowledge_note`, `record_confirmed_answer`) and still mutated the
    preference store and the subscription table. Three tools remembering is not a control; it is
    three tools that happened to.

    So the check moves to the boundary every tool passes through, over the *same*
    `side_effecting_tools()` set the plan gate uses — one set, two gates, no third list to keep in
    sync. The three ad-hoc checks are gone with it, and with them their tailored wording: a uniform
    refusal that cannot be forgotten beats a bespoke sentence that can.

    Raised rather than short-circuited into a result, so it travels the path
    `PlanNotApprovedError` already proves works — recorded by the audit middleware as an `error`
    outcome, and relayed verbatim to the model by `surface_authorization_denials`, which is exactly
    what a dry run needs the model to read.

    Raises:
        DryRunRefusal: When the turn is a dry run and the tool changes something. The body never
            runs.
    """
    refusal = dry_run_refusal(context.function.name)
    if refusal is not None:
        raise refusal
    await call_next()


@function_middleware
async def surface_authorization_denials(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Turn a denied call into its own clear result, instead of MAF's generic failure message.

    Without `include_detailed_errors` (MAF's default), *any* exception that escapes a tool call —
    `AuthorizationError` included — collapses into the same opaque "Error: Function failed.",
    with no exception text reaching the model at all (`agent_framework._tools`: the detailed
    message is gated behind that one client-wide flag). Turning that flag on globally was
    considered and rejected: chemclaw's own errors are written to be chemist-safe, but an
    *unexpected* exception (e.g. a database-driver error) can embed connection details in its
    message, and the flag cannot distinguish exception types — it would expose either both or
    neither. `AuthorizationError` is chemclaw's own, deliberately-worded, always-safe type
    (e.g. "X lacks a privileged role for Y"), so it alone is singled out here: caught, and its
    message becomes the tool's own successful result — verbatim, no gating — so the model can
    accurately tell the chemist *why* the call was refused instead of guessing at "a temporary
    service issue." Every other exception is left untouched, still falling through to MAF's
    generic (safe-by-omission) handling.

    Attach this *outside* both `enforce_tool_authz` and the audit middleware: the exception must
    still reach audit unchanged (so a denial is recorded as an `error` outcome, exactly as
    today) before this layer converts it into the value the model actually sees.
    """
    try:
        await call_next()
    except AuthorizationError as exc:
        context.result = denial_result(exc)


@function_middleware
async def surface_domain_errors(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Turn a tool's own bad-input or outage error into a clear result, not MAF's generic failure.

    Same gap as `surface_authorization_denials`, over the two known-safe exception types: every
    `ChemclawError` (`chemclaw.core.errors`) is chemclaw's own established "this input/data is
    invalid" contract, raised only with a deliberately-worded, caller-safe message (e.g.
    `expand_note`'s "no note with id 'X'" — it echoes back the id the model itself supplied,
    never internal state). A live e2e finding: `expand_note` citing a reaction whose note is a
    pending, unmerged PR-gate submission (an expected, recurring scenario — D-018) failed with
    MAF's opaque "Error: Function failed.", so the model could not tell "pending review" apart
    from "typo'd id" apart from "deleted note," and could only guess at what happened.
    `ChemclawError`'s many subclasses (`InvalidSmilesError`, `FingerprintError`, `NoteError`,
    ...) get the same treatment for free — every one of them is written to this same safe
    contract. Every other exception is left untouched, still falling through to MAF's generic
    (safe-by-omission) handling.

    `SubsystemUnavailableError` (`chemclaw.core.errors`) is the second type, and qualifies as safe
    for the same *deliberate wording* reason rather than by inheritance — it is not a
    `ChemclawError`, because "the broker is down" is the opposite claim to "your data is invalid"
    (see its docstring). It is raised by `chemclaw.core.temporal_client.connect` and by
    `agent/durable_tools.py`'s two job handlers (a Temporal RPC status other than NOT_FOUND). A
    count drifts — this one did, the moment those two were added — so the rule is a contract on
    raisers rather than an inventory: every message is written for a chemist, names the subsystem
    and what was lost, and keeps hostnames, ports and driver text on `__cause__`,
    with one hand-written sentence that names the subsystem and the consequence and nothing else:
    the driver text, the address and the port stay on `__cause__` for the log. A live finding again:
    an unreachable Temporal reached `request_development_report` as "Error: Function failed.", and
    the model answered by writing the entire development report by hand and presenting it as
    PR-gated. Telling it "the durable backend is down, nothing was queued" is the whole difference
    between a reported outage and a fabricated deliverable.

    Attach this alongside `surface_authorization_denials`, outside the audit middleware, for
    the same reason: the exception must still reach audit unchanged (recorded as an `error`
    outcome) before this layer converts it into what the model sees.
    """
    try:
        await call_next()
    except (ChemclawError, SubsystemUnavailableError) as exc:
        context.result = domain_error_result(exc)


@function_middleware
async def announce_tool_failures(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    """Tell the turn's event stream that a tool raised, then let the exception continue untouched.

    The two `surface_*` middlewares above decide what the *model* is told about a failed call;
    this decides what the *chemist* is told, which until now was nothing. The failure was already
    logged and audited, but the transcript the person actually reads showed only a gap — a turn
    that trailed off after three failing launches with no answer and no error was the live finding
    that motivated this (D-138).

    Attached innermost, closest to the tool body, so it sees the raw exception before either
    converter turns it into a result: whether the model was handed a readable explanation is a
    separate question from whether the step worked. Nothing is altered — the exception is
    re-raised, so audit still records the `error` outcome and the converters still run exactly as
    before. `str(exc)` is not shown to the model here and is only ever rendered in the trace, but
    it is truncated for the same reason `chemclaw.agent.audit` truncates: an unexpected exception's
    text
    can be long and is not written to be read.
    """
    try:
        await call_next()
    except Exception as exc:
        record_tool_failure(context.function.name, failure_detail(exc))
        raise


# --- the LangGraph wiring ------------------------------------------------------------------------
#
# Five wrappers over the four decisions above, in the same order and with the same attachment
# reasoning. The one structural difference is how a gate stops a call: MAF middleware mutates
# `context.result`, LangChain's returns a `ToolMessage` instead of calling `handler`. Both mean
# "the tool body never ran and this is what the model is told instead", so the difference is
# spelling, not semantics.


def _refusal_message(request: Any, text: str) -> ToolMessage:
    """A tool result the model reads as this call's answer, carrying the id it must reply to.

    `tool_call_id` is not optional bookkeeping: an assistant `tool_use` block with no matching
    `tool_result` is a malformed exchange that the provider rejects outright, so a gate that
    refused without echoing the id would turn a refusal into a dead turn.
    """
    return ToolMessage(content=text, tool_call_id=request.tool_call["id"], status="error")


@wrap_tool_call
async def lg_enforce_tool_authz(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Block a tool call the turn's user is not authorized for, else run it unchanged."""
    authorize_tool(request.tool_call["name"])
    return await handler(request)


@wrap_tool_call
async def lg_refuse_writes_on_dry_run(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse any side-effecting tool while the turn is a dry run (`refuse_writes_on_dry_run`)."""
    refusal = dry_run_refusal(request.tool_call["name"])
    if refusal is not None:
        raise refusal
    return await handler(request)


@wrap_tool_call
async def lg_surface_authorization_denials(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Hand a denial to the model verbatim (`surface_authorization_denials`).

    LangChain does not collapse tool exceptions into MAF's opaque "Function failed.", so the
    *reason* this exists differs between engines even though the behaviour is identical: there it
    recovers a message the framework threw away, here it keeps a deliberately-worded refusal from
    being reported as a tool error the model might retry. Attached outside the audit middleware
    either way, so the denial still reaches the trail as an `error` outcome first.
    """
    try:
        return await handler(request)
    except AuthorizationError as exc:
        return _refusal_message(request, denial_result(exc))


@wrap_tool_call
async def lg_surface_domain_errors(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Hand a bad-input or outage error to the model in its own words (`surface_domain_errors`)."""
    try:
        return await handler(request)
    except (ChemclawError, SubsystemUnavailableError) as exc:
        return _refusal_message(request, domain_error_result(exc))


@wrap_tool_call
async def lg_announce_tool_failures(request: Any, handler: Callable[[Any], Any]) -> Any:
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
