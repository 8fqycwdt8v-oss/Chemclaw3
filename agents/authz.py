"""Authorization decisions live in exactly one module (plan Phase F4-T5, F10-C).

Two gates, one home, so authorization is never scattered across tools and layers:

- `authorize_trigger` — the coarse gate for **expensive triggers** (a costly HPC/BO job): a
  job-launching tool calls it with the action name before starting the durable work, so an
  autonomously-planned todo cannot start an expensive path outside the requesting user's
  entitlements. Config: `entra_expensive_actions` × `entra_privileged_roles`.
- `authorize_tool` — the fine-grained gate applied to **every tool invocation** by one middleware
  (`agents.tool_authz`), generalizing the coarse gate so per-tool RBAC does not have to be hand-
  wired into each tool. Config: `tool_role_gates` (tool → allowed roles) + `tool_authz_default`.

Both read the turn's ambient identity (`agents.identity_context`) and are active only when
`entra_required` (a real deployment with real Entra roles); in local dev they are open, so the app
runs without a tenant. Both defer the same role-membership predicate to `_has_required_role`, so the
two gates can never drift in how "does this user hold an allowed role?" is decided (DRY).
"""

from agents.identity_context import get_current_actor, get_current_roles
from chemclaw.config import settings


class AuthorizationError(Exception):
    """The current user is not entitled to trigger the requested action."""


def _has_required_role(required: frozenset[str]) -> bool:
    """Whether the turn's user holds at least one of `required` (the shared membership predicate).

    An empty `required` means "no specific role needed" → always satisfied. Otherwise the turn's
    ambient roles must intersect it. One definition, used by both `authorize_trigger` (privileged
    roles for an expensive action) and `authorize_tool` (a tool's gate), so the two cannot drift.
    """
    if not required:
        return True
    return bool(get_current_roles() & required)


def authorize_tool(tool: str) -> None:
    """Authorize the current turn's user to invoke `tool`, or raise `AuthorizationError` (F10-C).

    Per-tool RBAC applied by `agents.tool_authz` to every tool call. Consults `tool_role_gates`
    (tool name → allowed roles) against the turn's ambient roles; a tool with no gate entry falls
    back to `tool_authz_default` (`"allow"` = today's behavior, `"deny"` = allowlist mode). The
    gate is active only under `entra_required`; in dev it is open.

    Args:
        tool: The tool's registered name (e.g. `"submit_qm_job"`, `"gather_evidence"`).

    Raises:
        AuthorizationError: When enforcement is on and the user is not permitted to call `tool` —
            either it is ungated under a `deny` default, or its gate lists roles the user lacks.
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    required = settings.tool_role_gates.get(tool)
    if required is None:
        if settings.tool_authz_default == "deny":
            raise AuthorizationError(f"{tool} is not in the tool allowlist (deny by default)")
        return  # not gated, allow-by-default
    if not _has_required_role(frozenset(required)):
        actor = get_current_actor() or "an unauthenticated user"
        raise AuthorizationError(f"{actor} lacks a role permitted to call {tool}")


def authorize_trigger(action: str) -> None:
    """Authorize the current turn's user to trigger `action`, or raise `AuthorizationError`.

    Args:
        action: The trigger's name (e.g. `"submit_qm_job"`). If it is not in
            `entra_expensive_actions`, the call is always allowed.

    Raises:
        AuthorizationError: When enforcement is on, the action is expensive, and the user holds none
            of the `entra_privileged_roles` (or there is no authenticated user at all).
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    if action not in settings.entra_expensive_action_set:
        return  # not a gated action
    actor = get_current_actor()
    if actor is None:
        raise AuthorizationError(f"{action} requires an authenticated user")
    if not _has_required_role(settings.entra_privileged_role_set):
        raise AuthorizationError(f"user {actor} lacks a privileged role for {action}")


def require_actor() -> str:
    """Return the turn's Entra actor for a user-triggered workflow, or raise if absent.

    Plan F4-T3 — the core rule: every *user-triggered* backend workflow is user-specific via
    Entra, so the requesting user's `oid` is a required, authorizing input. When `entra_required`
    (a real deployment), a trigger with no authenticated user is rejected here — reject-if-absent —
    before any durable work starts, mirroring how `require_canonical_smiles` rejects bad data at
    the durable boundary. This is the one reusable place that rule flows through: a job-launching
    tool calls it to populate `requested_by`.

    In local dev (no tenant) there is no authenticated user, so the configured `service_actor_id`
    stands in. System-triggered jobs (scheduled ELN sync, memory distillation) have no user and do
    not call this — they run as the service by design, not on behalf of a person.

    Returns:
        The authenticated user's Entra `oid`, or `settings.service_actor_id` when enforcement's off.

    Raises:
        AuthorizationError: When `entra_required` and there is no authenticated user in context.
    """
    actor = get_current_actor()
    if actor is not None:
        return actor
    if settings.entra_required:
        raise AuthorizationError("a user-triggered workflow requires an authenticated user")
    return settings.service_actor_id
