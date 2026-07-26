"""Per-tool authorization as one MAF function middleware (plan Phase F10-C).

Where `agents.audit` records every tool call, this **gates** every tool call: before a tool runs,
`enforce_tool_authz` asks `agents.authz.authorize_tool` whether the turn's user may invoke it, and
lets `AuthorizationError` propagate to block the call. It generalizes the single expensive-trigger
gate (F4-T5) so per-tool RBAC is applied uniformly by one interceptor, not hand-wired into each
tool — the same DRY move the audit trail makes.

The decision lives in `agents.authz` (the one home for authorization); this module is only the MAF
wiring, exactly as `agents.audit` is the wiring over the audit decision. It is safe to attach
unconditionally: `authorize_tool` is a no-op unless `entra_required`, so the classic/dev path is
unaffected (the gate is open with no tenant). Attach it *after* the audit middleware so a denied
attempt is still recorded as an `error` outcome before the exception surfaces.
"""

from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, function_middleware

from agents.authz import AuthorizationError, authorize_tool


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
        context.result = f"Refused: {exc}"
