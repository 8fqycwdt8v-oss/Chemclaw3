"""Authorization decisions live in exactly one module (plan Phase F4-T5, F10-C).

Two gates, one home, so authorization is never scattered across tools and layers:

- `authorize_trigger` — the coarse gate for **expensive triggers** (a costly HPC/BO job): a
  job-launching tool calls it with the action name before starting the durable work, so an
  autonomously-planned todo cannot start an expensive path outside the requesting user's
  entitlements. Config: `entra_expensive_actions` × `entra_privileged_roles`.
- `authorize_tool` — the fine-grained gate applied to **every tool invocation** by one middleware
  (`chemclaw.agent.tool_authz`), generalizing the coarse gate so per-tool RBAC does not have to be
  hand-
  wired into each tool. Config: `tool_role_gates` (tool → allowed roles) + `tool_authz_default`,
  with the built-in `DEFAULT_WRITE_TOOL_GATES` closing the write tools out of the box.

Both read the turn's ambient identity (`chemclaw.agent.identity_context`) and are active only when
`entra_required` (a real deployment with real Entra roles); in local dev they are open, so the app
runs without a tenant. Both defer the same role-membership predicate to `_has_required_role`, so the
two gates can never drift in how "does this user hold an allowed role?" is decided (DRY).
"""

from chemclaw.agent.identity_context import get_current_actor, get_current_roles
from chemclaw.core.config import settings


class AuthorizationError(Exception):
    """The current user is not entitled to trigger the requested action."""


# The write/side-effect tools gated to `entra_privileged_role_set` when the operator has NOT
# configured an explicit `tool_role_gates` entry for them. Under `tool_authz_default="allow"`
# every *read* tool stays open (the dev-friendly posture), but a tool that launches a job or
# mutates state must never be callable by any authenticated user just because nobody remembered
# to gate it — writes are closed by default, opened by explicit operator config. The index_*
# entries are defense in depth: the MCP `allowed_tools` boundary already keeps them off the
# agent (D-029), so this gate only matters if an operator ever widens that list.
DEFAULT_WRITE_TOOL_GATES: frozenset[str] = frozenset(
    {
        "compute_dft_energy",  # launches a durable HPC/DFT run
        "propose_knowledge_note",  # pushes a branch to the knowledge repo
        "record_confirmed_answer",  # pushes a branch to the knowledge repo
        "index_molecule",  # mutates the fingerprint index
        "index_reaction",  # mutates the fingerprint index
    }
)

# Every in-process tool that changes stored state or starts durable work — the set the harness's
# plan gate refuses under an unapproved plan (`chemclaw.agent.plan_gate`, D-164).
#
# **This is a superset of `DEFAULT_WRITE_TOOL_GATES`, and the two are deliberately not merged.**
# That set is the RBAC *fallback*: membership makes a tool require a privileged role under
# `entra_required` with no operator config, so widening it would silently narrow live deployments'
# access to tools they can call today. Whether a tool writes and whether an unconfigured deployment
# should close it out of the box are different questions with different blast radii, so they get
# different sets and this one is derived from that one rather than duplicating it.
#
# The plan gate's full set is this ∪ every enabled connector job ∪ every enabled template launcher,
# assembled in `plan_gate.gated_tools()`. Those two are structural — every declared job and every
# template starts durable work — so they need no list here and grow on their own.
STATE_CHANGING_TOOLS: frozenset[str] = (
    frozenset(
        {
            "propose_knowledge_note",  # pushes a branch to the knowledge repo
            "record_confirmed_answer",  # pushes a branch to the knowledge repo
            "remember_preference",  # writes user_preferences
            "forget_preference",  # deletes from user_preferences
            "watch_for",  # writes subscriptions
            "stop_watching",  # deletes from subscriptions
            "request_development_report",  # starts a durable report workflow
        }
    )
    | DEFAULT_WRITE_TOOL_GATES
)

# The in-process tools that only read. Not consulted at run time — the gate asks whether a tool is
# in `STATE_CHANGING_TOOLS`, and a name in neither set would simply be treated as a read.
#
# It exists so that cannot happen silently. `tests/test_authz.py` asserts that every name in
# `registered_tool_names()` falls in exactly one of the two sets, so adding a tool without
# classifying it fails the suite rather than shipping an ungated write. A hand-kept allow-list is
# only as good as the day it was written; a hand-kept *partition* is checked against reality on
# every run.
#
# The check runs over the registry, not over the union: `STATE_CHANGING_TOOLS` also names tools
# that are not in-process at all (`compute_dft_energy` is a connector job, `index_*` are MCP tools
# behind an `allowed_tools` boundary), inherited from `DEFAULT_WRITE_TOOL_GATES`. Those are correct
# entries and correctly absent from the registry.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "ask_clarifying_question",
        "expand_note",
        "find_knowledge_gaps",
        "find_notes",
        # A search over the durable job record (D-157). Emphatically a read, and one an agent
        # should make *before* asking for an expensive run to be authorized.
        "find_past_jobs",
        "gather_evidence",
        "get_durable_job_status",
        "list_attachments",
        "list_watches",
        "read_attachment",
        "recall_preferences",
    }
)


def _actor() -> str:
    """Name the turn's user for a refusal message, or say plainly that there isn't one."""
    return get_current_actor() or "an unauthenticated user"


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

    Per-tool RBAC applied by `chemclaw.agent.tool_authz` to every tool call. Consults
    `tool_role_gates`
    (tool name → allowed roles) against the turn's ambient roles. A tool with no gate entry
    follows `tool_authz_default`: under `"deny"` (allowlist mode) it is refused outright, and
    under `"allow"` it is open — except the built-in `DEFAULT_WRITE_TOOL_GATES`, which require a
    role from `entra_privileged_role_set` out of the box (an explicit operator gate overrides
    this). The built-in write gate only *narrows* the `"allow"` default; it never widens
    `"deny"` — a privileged role is not an allowlist entry. The gate is active only under
    `entra_required`; in dev it is open.

    Every refusal message is written for the **chemist**, because `chemclaw.agent.tool_authz` hands
    it to
    the model verbatim as the tool's result and the model relays it into the conversation. All
    three therefore say the same thing in the same shape — who was refused, which tool, and why —
    rather than describing the deployment's configuration. A live RBAC sweep found the cost of the
    old phrasing: the deny-default message ("not in the tool allowlist") read as an operator's note
    about a config file, so the model relayed a denial as "not currently available… a configuration
    issue", which tells a chemist to file a bug instead of requesting access. The operator's remedy
    (add an entry to `tool_role_gates`, or grant a privileged role) belongs in the runbook and this
    docstring, not in a message a chemist reads.

    Args:
        tool: The tool's registered name (e.g. `"compute_dft_energy"`, `"gather_evidence"`).

    Raises:
        AuthorizationError: When enforcement is on and the user is not permitted to call `tool` —
            its gate (explicit or built-in) lists roles the user lacks, or it is ungated under a
            `deny` default.
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    required = settings.tool_role_gates.get(tool)
    if required is not None:
        if not _has_required_role(frozenset(required)):
            raise AuthorizationError(
                f"{_actor()} is not authorized to use {tool}: the account holds none of the "
                "roles this tool requires"
            )
        return
    if settings.tool_authz_default == "deny":
        # Allowlist mode: not listed ⇒ refused, always. Checked *before* the built-in write
        # gate so a privileged role can never open an unlisted write tool under `deny` —
        # that would invert the allowlist for exactly the dangerous tools.
        raise AuthorizationError(
            f"{_actor()} is not authorized to use {tool}: this deployment permits only an "
            "approved list of tools, and this one is not on it"
        )
    if tool in DEFAULT_WRITE_TOOL_GATES:
        privileged = settings.entra_privileged_role_set
        # An empty privileged set means fail closed, not open: `_has_required_role` treats
        # "no roles required" as satisfied, which is right for operator gates but would
        # silently void the built-in write gate on an unconfigured deployment.
        if not privileged or not _has_required_role(privileged):
            raise AuthorizationError(
                f"{_actor()} is not authorized to use {tool}: it changes stored data, so it "
                "requires a privileged role the account does not hold"
            )


def authorize_trigger(action: str) -> None:
    """Authorize the current turn's user to trigger `action`, or raise `AuthorizationError`.

    Args:
        action: The trigger's name (e.g. `"compute_dft_energy"`). If it is not in
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
