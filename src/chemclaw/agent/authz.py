"""Authorization decisions live in exactly one module (plan Phase F4-T5, F10-C).

Two gates, one home, so authorization is never scattered across tools and layers:

- `authorize_trigger` — the coarse gate for **expensive triggers** (a costly HPC/BO job): a
  job-launching tool calls it with the action name before starting the durable work, so an
  autonomously-planned todo cannot start an expensive path outside the requesting user's
  entitlements. What it protects is `expensive_actions()` — every job a manifest declares
  `expensive: true`, plus whatever `entra_expensive_actions` adds — against
  `entra_privileged_roles`.
- `authorize_tool` — the fine-grained gate applied to **every tool invocation** by one middleware
  (`chemclaw.agent.tool_authz`), generalizing the coarse gate so per-tool RBAC does not have to be
  hand-
  wired into each tool. Config: `tool_role_gates` (tool → allowed roles) + `tool_authz_default`,
  with the built-in `default_write_tool_gates()` closing the write tools out of the box —
  core's own plus every enabled bundle's declared `privileged` subset.

Both read the turn's ambient identity (`chemclaw.core.identity_context`) and are active only when
`entra_required` (a real deployment with real Entra roles); in local dev they are open, so the app
runs without a tenant. Both defer the same role-membership predicate to `_has_required_role`, so the
two gates can never drift in how "does this user hold an allowed role?" is decided (DRY).
"""

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_roles


class AuthorizationError(Exception):
    """The current user is not entitled to trigger the requested action.

    Deliberately **not** a `ChemclawError` (hence not a `ValueError`): `ChemclawError`'s contract is
    "this input/data is invalid", and an authorization refusal says nothing about the data — the
    identical call succeeds for a different user with the identical arguments. Reparenting it here
    would also silently change what a chemist reads, not just the class hierarchy:
    `chemclaw.agent.tool_authz.surface_domain_errors` catches `ChemclawError` and answers
    `f"Error: {exc}"`, and it sits *inside* `surface_authorization_denials` in the real middleware
    chain (`chemclaw.agent.chemclaw_agent`) — meaning it would catch every `AuthorizationError`
    first and turn it into an `"Error: ..."`, never letting `surface_authorization_denials` answer
    its intended `"Refused: ..."`. The two middlewares exist specifically to keep those two
    messages apart.

    Still registered by exact class name in `chemclaw.durable.publish._BAD_DATA_TYPES`: Temporal
    matches `non_retryable_error_types` by that name, not by `isinstance`, so this class needs no
    `ValueError` ancestry to be non-retryable there — `chemclaw.durable.template_activities.
    authorize_job_step` raises it crossing a real activity boundary, and an authorization refusal
    never changes on retry, so it must fail fast there too. `DryRunRefusal` and
    `PlanNotApprovedError` are registered by their own names for the same reason, and
    `tests/test_publish.py` walks this hierarchy exactly as it walks `ChemclawError`'s, so a future
    subclass cannot go unregistered unnoticed.
    """


# The expensive actions **core itself owns**, as distinct from the ones a connector manifest
# declares with `expensive: true`.
#
# `expensive_actions()` derives its set from the enabled bundles, which is the right principle —
# the owner of the capability declares the fact, so a bundle added next year is gated the day it
# is enabled. But a durable job launched from core has no manifest to declare it, and exactly one
# exists: `request_development_report` starts an unbounded multi-section research workflow. Its
# `authorize_trigger` call was therefore inert on the shipped chart (`entra_required=true` with
# both role settings empty), and no other gate covered it — it is in `STATE_CHANGING_TOOLS` but
# not in the built-in write gate, so any authenticated user could launch one.
#
# Declared here rather than added to the chart's `entra_expensive_actions`, so that a deployment
# gets it without configuring anything — the same property the manifest derivation gives bundles.
CORE_EXPENSIVE_ACTIONS: frozenset[str] = frozenset({"request_development_report"})

# The write tools **core itself owns**, gated to `entra_privileged_role_set` when the operator has
# NOT configured an explicit `tool_role_gates` entry for them. Under `tool_authz_default="allow"`
# every *read* tool stays open (the dev-friendly posture), but a tool that launches a job or
# mutates state shared across users must never be callable by any authenticated user just because
# nobody remembered to gate it — writes are closed by default, opened by explicit operator config.
#
# Core's own, because a connector's writes are now *declared* by the bundle and derived from it
# (`default_write_tool_gates`). The index_* entries stay here despite belonging to `molfp`/`rxnfp`,
# and the reason is structural rather than an oversight: they are deliberately absent from those
# manifests' agent-facing `tools`, so they cannot be declared `privileged` there — a manifest may
# only classify what it serves the agent. They are defense in depth anyway, since the MCP
# `allowed_tools` boundary already keeps them off the agent (D-029); this gate matters only if an
# operator widens that list.
#
# `compute_dft_energy` is *not* here any more, and its absence is the derivation working rather than
# a hole: it is `qm`'s `expensive: true` job, so `expensive_actions()` gates it against the same
# predicate. Naming another bundle's tool in core was the second source of truth this closes.
CORE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "propose_knowledge_note",  # pushes a branch to the knowledge repo
        "record_confirmed_answer",  # pushes a branch to the knowledge repo
        "record_failure",  # pushes a branch to the knowledge repo, and retires a merged claim
        "index_molecule",  # mutates the fingerprint index
        "index_reaction",  # mutates the fingerprint index
    }
)

# Every in-process tool that changes stored state or starts durable work — the set the harness's
# plan gate refuses under an unapproved plan (`chemclaw.agent.plan_gate`, D-167).
#
# **This is a superset of the write gate's core half, and the two are deliberately not merged.**
# That set is the RBAC *fallback*: membership makes a tool require a privileged role under
# `entra_required` with no operator config, so widening it would silently narrow live deployments'
# access to tools they can call today. Whether a tool writes and whether an unconfigured deployment
# should close it out of the box are different questions with different blast radii, so they get
# different sets and this one is derived from that one rather than duplicating it.
#
# The same distinction is now declared on the connector side as `state_changing` vs `privileged`,
# and it is the same axis: `remember_preference` writes, and it writes *the asking chemist's own*
# preference, so gating it behind a privileged role would refuse a user their own settings.
#
# The *complete* side-effecting set is this ∪ every enabled connector job ∪ every enabled template
# launcher, assembled in `side_effecting_tools()` below. Those two are structural — every declared
# job and every template starts durable work — so they need no list here and grow on their own.
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
    | CORE_WRITE_TOOLS
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
# behind an `allowed_tools` boundary), inherited from `CORE_WRITE_TOOLS`. Those are correct
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
        # The ungated observations tier (D-161). A read of a table nothing but the durable
        # mining job writes, and its whole purpose is to point at evidence worth gathering
        # *before* anything is authorized.
        "recall_observations",
        "get_durable_job_status",
        "list_attachments",
        "list_watches",
        "read_attachment",
        "recall_preferences",
    }
)


def side_effecting_tools() -> frozenset[str]:
    """Every tool that changes something outside the turn — the one set the write gates share.

    Three sources, each owned where its knowledge lives:

    - `STATE_CHANGING_TOOLS` — the in-process writes, classified above and held to a partition of
      the tool registry by `tests/test_authz.py`;
    - every enabled connector's own declaration (`state_changing_tool_names`): its endpoint's
      declared `state_changing` subset, plus every declared job, since a job is durable work by
      construction. **This half is what made the plan gate cover DARK-1's live repro** —
      `compute_xtb_energy` is a `calc` *endpoint* tool, not a job, so a set built only from
      in-process names and job names would have missed one of the two things the unapproved turn
      actually ran;
    - every enabled template launcher — a template starts a fixed sequence of the above, and is the
      one thing that can reach a job step without the model naming the job.

    Nothing here is a list core maintains about other people's tools, which is the property that
    matters: a bundle added next year is gated the day it is enabled, not the day someone
    remembers. Imported lazily because the connector and template registries reach the agent
    builder, which reaches this module.

    **It lives here rather than beside one of its consumers.** It was born in
    `chemclaw.agent.plan_gate` as `gated_tools`, which read correctly while the plan gate was the
    only caller and stopped reading correctly the moment dry-run needed the same answer: dry-run
    applies whether or not the harness is on, so the definitive list of "tools that change things"
    cannot be owned by a harness module. It belongs where the classification it extends already
    lives.
    """
    from chemclaw.connectors.registry import state_changing_tool_names
    from chemclaw.templates.registry import template_tool_names

    return (
        STATE_CHANGING_TOOLS
        | frozenset(state_changing_tool_names())
        | frozenset(template_tool_names())
    )


def default_write_tool_gates() -> frozenset[str]:
    """Every tool the built-in RBAC write gate closes with no operator config.

    **The declaration the gate never consulted.** `DEFAULT_WRITE_TOOL_GATES` was a hand-maintained
    list in core while every manifest already classified its own tools, so a connector write was
    gated only if someone in core remembered it — and one was not. `report_measurement` writes the
    calibration ledger that every chemist's `calculator_trust` reads, and any authenticated user
    could call it.

    **What it derives from is `privileged`, not `state_changing`, and that is the correction the
    measurement forced.** Deriving from `state_changing` — the obvious reading, and the one the
    backlog row proposed — would have newly required a privileged role for **18 tools**, among them
    `predict_pka`, `compute_xtb_energy` and `suggest_next_experiment`: a bundle lists those as
    state-changing because they burn CPU and write a cache row, which is the right answer to the
    question the *plan gate* asks and the wrong answer to this one. An `entra_required` deployment
    that had not written a `tool_role_gates` entry would have lost its science. So the manifest
    gained a narrower declaration for the narrower question (`manifest._check_privileged`).

    Three sources, each owned where its knowledge lives:

    - `CORE_WRITE_TOOLS` — core's own writes, which have no manifest to declare them;
    - every enabled connector's declared `privileged` subset;
    - `expensive_actions()` — a job a bundle calls `expensive: true` is already refused to the same
      actors by `authorize_trigger`, against the identical predicate, so including it here changes
      no decision and lets core stop naming `compute_dft_energy`, which was never its tool.

    Imported lazily for the same reason `side_effecting_tools()` is: the connector registry reaches
    the agent builder, which reaches this module.

    **Recomputed per call, deliberately, and the number is here so it is not re-litigated.**
    `authorize_tool` calls this on every tool invocation and it walks the enabled manifests twice.
    Measured: **5.3 µs per call — 0.53 ms across a 100-tool-call turn**, against a turn dominated by
    model latency. Caching it would need invalidation wired to `discovered.cache_clear()` *and* to
    the thirteen tests that repoint `connectors_enabled`/`entra_expensive_actions`, which is real
    machinery bought with half a millisecond. The live read also has a property a cache would take
    away: a gate that reflects the config as it is now, with no second place for the two to
    disagree.
    """
    from chemclaw.connectors.registry import privileged_tool_names

    return CORE_WRITE_TOOLS | frozenset(privileged_tool_names()) | expensive_actions()


def expensive_actions() -> frozenset[str]:
    """Every action the coarse trigger gate protects — the declarations plus the operator's list.

    **`expensive: true` in a `connector.yaml` had authorized nothing.** `JobSpec.expensive` is
    documented as marking a job for this gate, `connectors.jobs.prepare_job_launch` dutifully calls
    `authorize_trigger(job.name)` for it, and `authorize_trigger` then returned immediately unless
    an operator had *separately* named that job in `entra_expensive_actions`. So the manifest flag
    was decoration: under `entra_required=True` with a role-less actor, `sample_conformers`,
    `compute_interaction_energy` and `start_optimization_campaign` all ran, and only
    `compute_dft_energy` was refused — by the built-in write gate's membership, a different gate
    that happens to name it. The shipped chart is precisely that shape: `entra_required=true` with
    both role settings left empty.

    Deriving the set instead is the same move `side_effecting_tools()` makes, for the same reason:
    the bundle owns the fact, so a capability added next year is gated the day it is enabled rather
    than the day someone remembers to extend a list in core. `entra_expensive_actions` remains, and
    remains a union — it is how an operator gates something the manifests do not call expensive.

    Imported lazily because the connector registry reaches the agent builder, which reaches this
    module.
    """
    from chemclaw.connectors.registry import enabled

    declared = frozenset(
        job.name for manifest in enabled() for job in manifest.jobs if job.expensive
    )
    return settings.entra_expensive_action_set | declared | CORE_EXPENSIVE_ACTIONS


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
    under `"allow"` it is open — except the built-in `default_write_tool_gates()`, which require a
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
    if tool in default_write_tool_gates():
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
            `expensive_actions()`, the call is always allowed.

    Raises:
        AuthorizationError: When enforcement is on, the action is expensive, and the user holds none
            of the `entra_privileged_roles` (or there is no authenticated user at all, or the
            deployment declared no privileged role for an expensive action to require).
    """
    if not settings.entra_required:
        return  # dev: no tenant, open gate
    if action not in expensive_actions():
        return  # not a gated action
    actor = get_current_actor()
    if actor is None:
        raise AuthorizationError(f"{action} requires an authenticated user")
    privileged = settings.entra_privileged_role_set
    # An empty privileged set means fail closed, not open — the same rule the built-in write gate
    # in `authorize_tool` states, and now reachable for the same reason: `_has_required_role` treats
    # "no roles required" as satisfied, which is right for an operator's own gate and would void
    # this one entirely on the shipped chart, where `entra_required` is on and neither role setting
    # is filled in. Config validation cannot catch it — it requires `entra_privileged_roles`
    # whenever `entra_expensive_actions` names anything, and a *declared* expensive job needs no
    # entry in either, so the shipped shape passes validation with both empty.
    if not privileged or not _has_required_role(privileged):
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
