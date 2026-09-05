"""The single authorization gate for expensive triggers (plan Phase F4-T5), offline.

Proves `authorize_trigger` allows/denies by the turn's ambient roles per config, and that the audit
trail attributes to the real ambient actor — all with fakes, no Temporal or tenant.

The *launcher* half — that an expensive job authorizes and stamps the requesting user before any
durable work — moved with the launchers themselves: every durable capability is a declared
connector job now (D-118), so `tests/test_connector_jobs.py` proves it once for all of them
instead of once per hand-written tool.
"""

import pytest

from chemclaw.agent.authz import (
    DEFAULT_WRITE_TOOL_GATES,
    READ_ONLY_TOOLS,
    STATE_CHANGING_TOOLS,
    AuthorizationError,
    authorize_tool,
    authorize_trigger,
    expensive_actions,
    require_actor,
)
from chemclaw.agent.profiles import get_profile
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from tests.surface import surface


def _privileged_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "sample_conformers")
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")


def test_dev_mode_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, every trigger is allowed (local dev, no tenant)."""
    monkeypatch.setattr(settings, "entra_required", False)
    authorize_trigger("sample_conformers")  # does not raise


def test_non_expensive_action_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action not declared expensive is allowed even under enforcement."""
    _privileged_env(monkeypatch)
    authorize_trigger("find_notes")  # not in the expensive set → allowed


def test_privileged_role_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user holding a privileged role may trigger the expensive action."""
    _privileged_env(monkeypatch)
    token = set_current_identity("u-1", frozenset({"compute"}))
    try:
        authorize_trigger("sample_conformers")  # does not raise
    finally:
        reset_current_identity(token)


def test_missing_role_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user without a privileged role cannot trigger the expensive action.

    Matched on the message, not just the type. This test and `test_no_user_is_forbidden` below are
    the only two refusals `authorize_trigger` has, and both raise `AuthorizationError` — so a bare
    `pytest.raises(AuthorizationError)` in each is satisfied by *either* refusal firing twice.
    Deleting the `if actor is None` block entirely left both green, because an unauthenticated turn
    then fell through to the role check and was refused there anyway (measured).
    """
    _privileged_env(monkeypatch)
    token = set_current_identity("u-2", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError, match="user u-2 lacks a privileged role"):
            authorize_trigger("sample_conformers")
    finally:
        reset_current_identity(token)


def test_no_user_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under enforcement, an expensive action with no authenticated user is rejected.

    Rejected *for being unauthenticated*, which is the distinction the message carries and the
    reason the check is worth having: "no authenticated user" and "this user lacks a role" are
    different operator problems, and an audit line that says the second when the first happened
    sends whoever reads it to the wrong console.
    """
    _privileged_env(monkeypatch)
    with pytest.raises(AuthorizationError, match="requires an authenticated user"):
        authorize_trigger("sample_conformers")


def test_require_actor_returns_the_ambient_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authenticated user's oid is returned for attribution on a user-triggered workflow."""
    monkeypatch.setattr(settings, "entra_required", True)
    token = set_current_identity("u-oid", frozenset({"compute"}))
    try:
        assert require_actor() == "u-oid"
    finally:
        reset_current_identity(token)


def test_require_actor_falls_back_to_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off and no user, the configured service identity stands in (no reject)."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "service_actor_id", "svc-1")
    assert require_actor() == "svc-1"


def test_require_actor_rejects_absent_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core rule: under Entra, a user-triggered workflow with no user is rejected."""
    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(AuthorizationError):
        require_actor()


# --- `expensive: true` is the gate's source, not a comment ---------------------------------------


def _declared_expensive_jobs() -> set[str]:
    """Every job the enabled bundles declare `expensive: true`, read from the manifests."""
    from chemclaw.connectors.registry import enabled

    return {job.name for manifest in enabled() for job in manifest.jobs if job.expensive}


def test_every_declared_expensive_job_is_in_the_effective_gate_set() -> None:
    """A manifest's `expensive: true` must gate the job, with no operator entry to remember.

    It did not. `authorize_trigger` consulted `entra_expensive_actions` alone, so the declaration
    authorized nothing and a bundle marking a job expensive got a comment rather than a gate — the
    live shape being `entra_required=true` with both role settings empty, exactly what the shipped
    chart renders. This checks the two against each other, so a bundle added later cannot regress
    it: the property being pinned is that the *declaration* is what the gate reads.
    """
    declared = _declared_expensive_jobs()
    assert declared, "no enabled bundle declares an expensive job; this test would prove nothing"
    assert declared <= expensive_actions(), (
        "these jobs declare `expensive: true` and are not in the effective trigger gate, so they "
        f"start for any authenticated user: {sorted(declared - expensive_actions())}"
    )


def test_a_declared_expensive_job_is_refused_on_the_shipped_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chart's own shape — enforcement on, neither role setting filled in — must fail closed.

    Two failures composed here. The gate never saw a declared job at all, and even once it does,
    `_has_required_role` reads an empty requirement as "no specific role needed" and would allow
    every one of them. `authorize_tool` already states the rule for its built-in write gate; a
    trigger gate that says a job needs a privileged role must not allow it where none exists.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "")
    monkeypatch.setattr(settings, "entra_privileged_roles", "")
    token = set_current_identity("u-3", frozenset({"process-chemist"}))
    try:
        for job in sorted(_declared_expensive_jobs()):
            with pytest.raises(AuthorizationError, match="privileged role"):
                authorize_trigger(job)
    finally:
        reset_current_identity(token)


def test_a_declared_expensive_job_is_allowed_with_a_privileged_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate entitles rather than forbids: the declared job runs for a privileged user.

    The counterweight to the test above — a fail-closed gate that never opens is a broken
    capability, not a control.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "")
    monkeypatch.setattr(settings, "entra_privileged_roles", "calc-operator")
    token = set_current_identity("u-4", frozenset({"calc-operator"}))
    try:
        for job in sorted(_declared_expensive_jobs()):
            authorize_trigger(job)  # does not raise
    finally:
        reset_current_identity(token)


# --- the write/read classification, held to the registry it describes (D-167) ------------------


def test_every_advertised_tool_is_classified_write_or_read() -> None:
    """A new tool must be classified, or this fails — the gate cannot infer what a tool does.

    `chemclaw.agent.authz.side_effecting_tools` is what the harness's plan gate refuses under an
    unapproved plan, and a name it does not know is silently treated as a read. That is the failure
    mode this test exists to make impossible: an ungated write ships looking exactly like a gated
    one, and nothing about the running system says otherwise.

    The classification has three sources and the test checks all three at once, because the whole
    surface is what has to be covered: the in-process sets here, each connector's own
    `state_changing` declaration plus its jobs, and the template launchers. Checking only the first
    would have passed while `compute_xtb_energy` — a `calc` endpoint tool, and one of the two
    things the live unapproved turn actually ran — sat unclassified.

    `build_agent` is called first because that is what registers the job and template launchers
    into the shared registry; without it the registry holds only the `@tool` functions and the test
    would silently check a third of what it claims to.
    """
    from chemclaw.agent.authz import side_effecting_tools
    from chemclaw.core.tool_registry import registered_tool_names

    surface(None)
    advertised = set(registered_tool_names())
    classified = side_effecting_tools() | READ_ONLY_TOOLS
    assert advertised - classified == set(), (
        "these advertised tools are classified neither state-changing nor read-only, so the "
        "harness plan gate treats them as reads; add an in-process tool to one of the two sets in "
        "chemclaw.agent.authz, and a connector tool to its bundle's `endpoint.state_changing`"
    )
    assert not (advertised & STATE_CHANGING_TOOLS & READ_ONLY_TOOLS), (
        "a tool cannot be both a write and a read"
    )


#: The bound tools the write/read partition does **not** classify, each with what recovers it.
#:
#: Every one comes from a middleware rather than from `core/tool_registry`, so no walk of the
#: registry can see it — which is why the test above, held over `registered_tool_names()`, was
#: green while **7 of 61** bound tools sat outside the partition it exists to make complete. That is
#: the same basis defect `tests/test_context_floor.py::_bound_tools` was written to end: a set
#: re-derived from the callables agrees with itself forever, and only the compiled graph says what
#: the model was actually offered.
#:
#: They are recorded rather than classified, and each entry is a decision:
#:
#: - `write_file` / `edit_file` — recovered by `writes_durable_memory`, which reads the *path*.
#:   Classifying the names would refuse a turn its own `/scratch/` notepad under an unapproved plan,
#:   which is the case that predicate exists for.
#: - `read_file` / `ls` / `glob` / `grep` — reads of the same three roots. They would be honest
#:   entries in `READ_ONLY_TOOLS`, and are left here instead so this set is one thing —
#:   everything the registry cannot see — rather than two.
#: - `task` — recovered twice over, and neither recovery is a classification. `subagents.helper_
#:   profile` subtracts `side_effecting_tools()` from what a helper may hold, and the helper's own
#:   graph is compiled by `build_langgraph_agent`, so every call it makes re-enters this same chain
#:   under the same actor. Calling it state-changing would gate every delegation on plan approval;
#:   calling it read-only would assert a helper cannot write, which is true of today's roster rather
#:   than of the tool.
_UNCLASSIFIED_BOUND_TOOLS = frozenset(
    {"task", "ls", "read_file", "write_file", "edit_file", "glob", "grep"}
)


def test_every_tool_the_graph_binds_is_classified_or_recorded() -> None:
    """The partition has to cover what the model is offered, not what the registry happens to hold.

    `test_every_advertised_tool_is_classified_write_or_read` above reads
    `registered_tool_names()`, and `FilesystemMiddleware` and `SubAgentMiddleware` put seven tools
    on the graph that never pass through that registry. So the one assertion that exists to stop an
    ungated write shipping silently could not see the tool whose whole job is to run other tools.

    The surface is read off the compiled graph's `ToolNode`, the way `tests/test_context_floor.py`
    reads it, so a future tool source lands here the moment it is bound rather than when somebody
    remembers to teach a walk about it.

    **Both directions**, which is what makes the recorded set an argument rather than a mute. A new
    unclassified bound tool fails; so does a name in `_UNCLASSIFIED_BOUND_TOOLS` that has since been
    classified, so the record cannot outlive its reason.
    """
    from chemclaw.agent.authz import side_effecting_tools
    from tests.test_context_floor import _bound_tools

    surface(None)
    bound = {str(tool.name) for tool in _bound_tools(get_profile("default"))}
    classified = side_effecting_tools() | READ_ONLY_TOOLS

    assert bound - classified - _UNCLASSIFIED_BOUND_TOOLS == set(), (
        f"these bound tools are classified neither state-changing nor read-only: "
        f"{sorted(bound - classified - _UNCLASSIFIED_BOUND_TOOLS)}. The harness plan gate treats "
        "an unknown name as a read, so an ungated write would ship looking exactly like a gated "
        "one. Classify it in chemclaw.agent.authz, or add it to _UNCLASSIFIED_BOUND_TOOLS above "
        "with the argument for why a name cannot decide it."
    )
    assert _UNCLASSIFIED_BOUND_TOOLS & classified == set(), (
        f"{sorted(_UNCLASSIFIED_BOUND_TOOLS & classified)} are now classified, so their entry in "
        "_UNCLASSIFIED_BOUND_TOOLS is a stale claim — delete it in the same commit"
    )
    assert _UNCLASSIFIED_BOUND_TOOLS <= bound, (
        f"{sorted(_UNCLASSIFIED_BOUND_TOOLS - bound)} are recorded as unclassified bound tools and "
        "are not bound any more — delete them"
    )


def test_the_write_gate_is_a_subset_of_the_state_changing_set() -> None:
    """The RBAC fallback is narrower than the plan gate's set, and must stay inside it.

    The two are separate on purpose — membership of `DEFAULT_WRITE_TOOL_GATES` costs an
    unconfigured deployment access to a tool, so it is not widened lightly — but a tool that closes
    by default under RBAC and is *not* considered state-changing by the plan gate would be an
    outright contradiction.
    """
    assert DEFAULT_WRITE_TOOL_GATES <= STATE_CHANGING_TOOLS


def test_an_operators_empty_role_list_opens_a_tool_rather_than_closing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool_role_gates: {tool: []}` means "no role needed", and that convention is now pinned.

    Found by mutation testing (2026-08-04): flipping `_has_required_role`'s `if not required:
    return True` to `return False` survived every test in this file. The line is reachable in
    exactly one way — an operator listing a tool with an empty role list — and nothing exercised
    it, so the convention was decided by one unasserted branch.

    It is worth pinning precisely because the file's own comments state the *opposite* rule two
    lines away: an empty `entra_privileged_role_set` fails **closed** ("An empty privileged set
    means fail closed, not open"), and both privileged gates short-circuit on `not privileged`
    before ever reaching this predicate. The asymmetry is deliberate — an operator who writes
    `[]` against a tool has said something, whereas an unfilled chart default has not — but a
    deliberate asymmetry that no test can tell from an accident is one refactor from being
    "simplified" into a security change.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_authz_default", "deny")
    monkeypatch.setattr(settings, "tool_role_gates", {"find_notes": []})
    tokens = set_current_identity(actor="chemist@example.com", roles=frozenset())
    try:
        authorize_tool("find_notes")  # explicitly gated with no role required → allowed
        # And the deny default still governs everything the operator did not list, so this is a
        # statement about the empty list rather than about the gate being off.
        with pytest.raises(AuthorizationError, match="approved list of tools"):
            authorize_tool("gather_evidence")
    finally:
        reset_current_identity(tokens)


def test_a_non_empty_role_list_still_refuses_an_account_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same predicate: a listed role the account lacks is a refusal.

    Stated beside the test above so the pair reads as one decision. Without it, "empty means open"
    could be satisfied by a gate that was open to everyone.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_authz_default", "allow")
    monkeypatch.setattr(settings, "tool_role_gates", {"find_notes": ["chem-lead"]})
    tokens = set_current_identity(actor="chemist@example.com", roles=frozenset({"chem-reader"}))
    try:
        with pytest.raises(AuthorizationError, match="roles this tool requires"):
            authorize_tool("find_notes")
    finally:
        reset_current_identity(tokens)
    tokens = set_current_identity(actor="lead@example.com", roles=frozenset({"chem-lead"}))
    try:
        authorize_tool("find_notes")  # holds the listed role → allowed
    finally:
        reset_current_identity(tokens)


def _authorize_trigger_literals() -> dict[str, str]:
    """Every `authorize_trigger("literal")` in `src/`, as `{action: file:line}`.

    AST rather than grep, so a call spelled across two lines or nested inside a `try` is still
    found, and so a mention in a docstring or a comment is not.
    """
    import ast
    import pathlib

    found: dict[str, str] = {}
    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "chemclaw"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "authorize_trigger" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found[first.value] = f"{path.relative_to(root.parent.parent)}:{node.lineno}"
    return found


def test_every_hardcoded_authorize_trigger_action_is_actually_gated() -> None:
    """A gate that names an action nothing gates is decoration, and this repo has had two.

    `expensive_actions()` derives its set from the enabled bundles' manifests, which is right for
    connector jobs — `connectors/jobs.py` passes `job.name`, so a bundle added next year is gated
    the day it is enabled. But a job launched from *core* has no manifest to declare it, and
    `request_development_report` was exactly that: it calls `authorize_trigger`, the call returned
    immediately on the shipped chart (`entra_required=true`, both role settings empty), and no
    other gate covered it — `STATE_CHANGING_TOOLS` yes, `DEFAULT_WRITE_TOOL_GATES` no. Any
    authenticated user could start an unbounded multi-section research workflow.

    D-2026-08-01 fixed the same shape for manifests and left this one, because nothing checked the
    call sites against the set. This is that check: every literal action name passed to
    `authorize_trigger` anywhere in `src/` must resolve to something the gate actually protects.
    Dynamic call sites (`job.name`) are skipped deliberately — the derivation covers those, and it
    is the hardcoded ones that can silently name nothing.
    """
    gated = expensive_actions()
    literals = _authorize_trigger_literals()
    assert literals, "found no authorize_trigger call sites — the AST walk stopped working"
    ungated = {action: where for action, where in literals.items() if action not in gated}
    assert not ungated, (
        "authorize_trigger names action(s) that nothing gates, so the call is inert: "
        f"{ungated}. Declare them in CORE_EXPENSIVE_ACTIONS (core-owned) or via a manifest's "
        "`expensive: true` (bundle-owned)."
    )
