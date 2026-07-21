"""Tool-authorization enforcement at the agent's in-process tool boundary (Phase 6, plan 6.1).

`architektur.md` §8 wants authorization centralized where the caller's token is present. The
expensive actions (`submit_qm_job`, the calculators, BO) are **in-process agent tools** rather
than MCP tools (a deliberate KISS decision), so the single enforcement point is a MAF function
middleware over the tool boundary — the same seam the audit trail uses (D-027): one policy for
every tool, not a check copied into each (DRY).

A tool is *gated* by config (`settings.tool_role_gates`: tool name → allowed roles). A tool not
listed is ungated — any caller may run it; a listed tool runs only for a caller holding one of
its roles. An anonymous caller (no principal) holds no roles, so it is denied any gated tool.
Denial raises `ToolNotAuthorizedError`; because the audit middleware wraps this one (it is first
in `build_agent`'s middleware list, hence outermost), the denied attempt is recorded in the GxP
trail before the error propagates back to the model as the tool's failure.
"""

from collections.abc import Awaitable, Callable, Mapping

from agent_framework import FunctionInvocationContext, function_middleware

from chemclaw.identity import Principal


class ToolNotAuthorizedError(PermissionError):
    """A caller lacking the required role attempted a gated tool."""

    def __init__(self, tool: str, required: frozenset[str]) -> None:
        """Record which tool was blocked and which roles would have permitted it."""
        self.tool = tool
        self.required = required
        super().__init__(f"not authorized to call {tool!r}: requires one of {sorted(required)}")


def authorize(principal: Principal | None, tool: str, gates: Mapping[str, list[str]]) -> None:
    """Raise `ToolNotAuthorizedError` if `principal` may not call `tool`.

    Ungated tools (absent from `gates`) are always allowed. A gated tool requires the caller to
    hold one of its roles; an anonymous caller (`None`) holds none and is denied.

    Args:
        principal: The validated caller, or `None` for an anonymous/dev call.
        tool: The tool's registered function name.
        gates: Map of tool name → roles allowed to call it.
    """
    required = gates.get(tool)
    if required is None:
        return
    roles = principal.roles if principal is not None else frozenset()
    if not (roles & set(required)):
        raise ToolNotAuthorizedError(tool, frozenset(required))


def make_authz_middleware(
    *, principal: Principal | None, gates: Mapping[str, list[str]]
) -> Callable[[FunctionInvocationContext, Callable[[], Awaitable[None]]], Awaitable[None]]:
    """Build the tool-authorization middleware for one caller.

    Enforces `authorize` before each tool runs; a denial skips the tool and raises. Bound to one
    conversation's `principal`, like the audit middleware is bound to its identity.
    """

    @function_middleware
    async def enforce_tool_roles(
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        """Authorize the call against the caller's roles, then run the tool (or raise)."""
        authorize(principal, context.function.name, gates)
        await call_next()

    return enforce_tool_roles
