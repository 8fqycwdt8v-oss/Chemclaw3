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

from agents.authz import authorize_tool


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
