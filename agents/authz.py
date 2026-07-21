"""The single authorization gate for expensive triggers (plan Phase F4-T5).

Once the harness can autonomously plan and execute, "may this user launch this costly HPC/BO job?"
must be answered in exactly one place — not scattered across tools and layers. `authorize_trigger`
is that place: a job-launching tool calls it with the action name before starting the durable work,
and it consults the turn's ambient identity (`agents.identity_context`) against config. This keeps
authorization a single reusable piece (like the PR-gate and the retriever interface), so an
autonomously-planned todo cannot start an expensive path outside the requesting user's entitlements.

The gate is active only when `entra_required` (a real deployment with real Entra roles); in local
dev it is open, so the app runs without a tenant. An action not declared expensive is allowed.
"""

from agents.identity_context import get_current_actor, get_current_roles
from chemclaw.config import settings


class AuthorizationError(Exception):
    """The current user is not entitled to trigger the requested action."""


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
    if not (get_current_roles() & settings.entra_privileged_role_set):
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
