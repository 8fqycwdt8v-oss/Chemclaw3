"""The single authorization gate for expensive triggers, offline.

`authorize_trigger` allows or denies by the turn's ambient roles per config, and the audit trail
attributes to the real ambient actor. That a connector job authorizes before durable work is
proven once in `tests/test_connector_jobs.py`.
"""

from typing import Any, cast

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
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    get_current_actor,
    reset_current_identity,
    set_current_identity,
)
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

    Matched on the message: both refusals raise `AuthorizationError`, so a type check alone could be
    satisfied by the wrong one.
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

    Rejected for being unauthenticated: "no user" and "lacks a role" are different operator
    problems.
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


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t "])
def test_a_blank_actor_is_no_actor_and_not_a_new_person(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    r"""Every spelling of nothing is refused, not only `""`.

    `get_current_actor` is the fail-closed point every gate shares. A truthy blank would be treated
    as an authenticated user and get its own memory namespace — one nobody can erase. Parametrised
    over five spellings.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    token = set_current_identity(blank, frozenset({"compute"}))
    try:
        assert get_current_actor() is None, (
            f"{blank!r} was returned as an authenticated actor, so every gate that asks "
            "`is not None` sees a person and every namespace keyed on it is unattributable"
        )
        with pytest.raises(AuthorizationError, match="requires an authenticated user"):
            require_actor()
    finally:
        reset_current_identity(token)


def test_one_actor_with_stray_whitespace_is_one_memory_namespace() -> None:
    r"""`" oid "` and `"oid"` are one person and must not be two prefixes.

    `memory_namespace` digests what it is given, so the shared reader normalises. Asserted through
    `get_current_actor`, the path `scratchpad.durable_backend` takes.
    """
    from chemclaw.agent.scratchpad import memory_namespace

    namespaces = set()
    for spelling in (" oid-alice ", "oid-alice", "\toid-alice\n"):
        token = set_current_identity(spelling, frozenset())
        try:
            actor = get_current_actor()
            assert actor is not None
            namespaces.add(memory_namespace(actor))
        finally:
            reset_current_identity(token)

    assert len(namespaces) == 1, (
        f"one actor spelled three ways owns {len(namespaces)} memory namespaces; an erasure "
        "request names the person and can only reach one of them"
    )


# --- `expensive: true` is the gate's source, not a comment ---------------------------------------


def _declared_expensive_jobs() -> set[str]:
    """Every job the enabled bundles declare `expensive: true`, read from the manifests."""
    from chemclaw.connectors.registry import enabled

    return {job.name for manifest in enabled() for job in manifest.jobs if job.expensive}


def test_every_declared_expensive_job_is_in_the_effective_gate_set() -> None:
    """A manifest's `expensive: true` must gate the job, with no operator entry to remember.

    The declaration is what the gate reads, checked against every enabled bundle so a later bundle
    cannot regress it.
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

    `_has_required_role` reads an empty requirement as "no role needed", so the trigger gate must
    refuse a declared expensive job when no privileged role exists.
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

    An unknown name in `side_effecting_tools` is treated as a read, so an ungated write would look
    gated. All three sources are checked: the in-process sets, each connector's `state_changing`
    declaration and jobs, and the template launchers. `build_agent` runs first to register the
    launchers.
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


def test_the_write_gate_is_a_subset_of_the_state_changing_set() -> None:
    """The RBAC fallback is narrower than the plan gate's set, and must stay inside it.

    A tool closed by default under RBAC but not state-changing for the plan gate would be a
    contradiction.
    """
    assert DEFAULT_WRITE_TOOL_GATES <= STATE_CHANGING_TOOLS


def test_an_operators_empty_role_list_opens_a_tool_rather_than_closing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool_role_gates: {tool: []}` means "no role needed", and that convention is pinned.

    Deliberately asymmetric with an empty `entra_privileged_role_set`, which fails closed: an
    operator writing `[]` against a tool has said something; an unfilled chart default has not.
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
    """Every hardcoded `authorize_trigger` action is actually gated.

    Manifest-declared jobs derive their gate, but a core-launched job has no manifest, so every
    literal action name passed to `authorize_trigger` in `src/` must resolve to a protected action.
    Dynamic call sites (`job.name`) are covered by the derivation and skipped.
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


def _chart_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped chart's identity posture: enforcement on, every role list empty.

    What a real deployment reaches unless an operator names a role.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_authz_default", "allow")
    monkeypatch.setattr(settings, "tool_role_gates", {})
    monkeypatch.setattr(settings, "entra_privileged_roles", "")
    monkeypatch.setattr(settings, "entra_expensive_actions", "")


def _refused(gate: Any, name: str) -> bool:
    """Whether `gate` refuses `name` for the identity currently bound."""
    try:
        gate(name)
    except AuthorizationError:
        return True
    return False


def test_the_built_in_write_gate_closes_three_knowledge_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RBAC fallback covers three names, and every template launcher passes both gates.

    Expressed as set equalities, not counts, because the side-effecting surface grows with enabled
    bundles. A role-less authenticated user passes both RBAC gates for every launcher; the plan gate
    refuses it (next test, and
    `D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest`). Widening
    either gate turns this red.
    """
    from chemclaw.agent.authz import side_effecting_tools
    from chemclaw.templates.registry import template_tool_names

    surface(None)  # registers the job and template launchers into the shared registry
    _chart_posture(monkeypatch)
    token = set_current_identity(actor="chemist@example.com", roles=frozenset())
    try:
        gated = side_effecting_tools()
        by_tool_gate = {name for name in gated if _refused(authorize_tool, name)}
        by_trigger_gate = {name for name in gated if _refused(authorize_trigger, name)}
    finally:
        reset_current_identity(token)

    assert by_tool_gate == set(DEFAULT_WRITE_TOOL_GATES), (
        "`authorize_tool` refuses a role-less user exactly `DEFAULT_WRITE_TOOL_GATES` and nothing "
        "else; if that changed, the set's comment in chemclaw.agent.authz changed with it"
    )
    assert by_trigger_gate == set(expensive_actions()) & set(gated), (
        "an empty `entra_privileged_roles` fails closed over exactly the declared-expensive set"
    )
    launchers = frozenset(template_tool_names())
    open_at_both = gated - by_tool_gate - by_trigger_gate
    assert launchers, "no template launchers registered — the surface call stopped working"
    assert launchers <= open_at_both, (
        "a template launcher is now refused by an RBAC gate for a role-less user. That is a "
        "posture change, not a test failure: update DEFAULT_WRITE_TOOL_GATES' comment, which says "
        "the plan gate is what carries the launchers."
    )


def test_the_plan_gate_refuses_a_launcher_the_rbac_gates_leave_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan gate refuses a launcher the RBAC gates leave open.

    Driven on the same posture through the real middleware and approval store: a launcher under an
    unapproved plan raises.
    """
    import asyncio

    from chemclaw.agent import plan_approval_store as store_module
    from chemclaw.agent.plan_gate import PlanNotApprovedError, enforce_plan_approval
    from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
    from chemclaw.templates.registry import template_tool_names
    from tests.middleware import run_middleware, tool_request

    surface(None)
    _chart_posture(monkeypatch)
    monkeypatch.setattr(settings, "session_store", "memory")
    store_module.plan_approval_store.cache_clear()
    launcher = sorted(template_tool_names())[0]

    async def _run() -> bool:
        ran = False

        async def _handler(_request: Any) -> Any:
            nonlocal ran
            ran = True
            return None

        request = tool_request(launcher)
        object.__setattr__(request, "state", {"todos": [{"content": "run the launcher"}]})
        token = set_current_session_id("s-unapproved")
        identity = set_current_identity(actor="chemist@example.com", roles=frozenset())
        try:
            await run_middleware(enforce_plan_approval, request, _handler)
        finally:
            reset_current_identity(identity)
            reset_current_session_id(token)
        return ran

    try:
        with pytest.raises(PlanNotApprovedError):
            asyncio.run(_run())
    finally:
        store_module.plan_approval_store.cache_clear()


def _privileged_roles_section() -> str:
    """The `deploy/README.md` section documenting the empty `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "deploy" / "README.md").read_text()
    marker = "### The setting that does *not* block boot"
    start = readme.index(marker)
    end = readme.index("\n## ", start + len(marker))
    return readme[start:end]


def test_the_operator_note_lists_no_expensive_job_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The blast-radius section derives its list instead of holding one, and the command works.

    Most expensive jobs are declared by bundles served from `Chemclaw3-mcp`, so a hand list goes
    stale. The README names a command; this runs its payload, lifted from the README, against the
    live set, and fails if a hand list returns.
    """
    import io
    import re
    from contextlib import redirect_stdout

    surface(None)  # registers the bundles whose manifests declare `expensive: true`
    monkeypatch.setattr(settings, "entra_expensive_actions", "")
    section = _privileged_roles_section()

    payload = re.search(r'uv run python -c "(.+?)"\n', section)
    assert payload is not None, (
        "the section no longer names a `uv run python -c` command an operator can run to print "
        "what this deployment closes; it must not go back to listing jobs"
    )
    printed = io.StringIO()
    with redirect_stdout(printed):
        # The README's own line, executed as an operator would run it.
        exec(payload.group(1), {})
    assert printed.getvalue().split() == sorted(expensive_actions()), (
        "the documented command no longer prints the set the trigger gate protects"
    )

    named = sorted(action for action in expensive_actions() if f"`{action}`" in section)
    assert named == [], (
        f"{named} are named in deploy/README.md's blast-radius section. That list is derived from "
        "manifests this repository does not own, so naming any of it here is a claim that goes "
        "stale on a bundle merge — point at the command instead."
    )


# --- the durable-memory write gate, and the upstream behaviour it is coupled to ------------------


#: The spellings of one durable memory path that `os.path.normpath` collapses onto each other, plus
#: the bare root upstream routes without a trailing slash. Built **from** `MEMORY_ROOT` rather than
#: written out, so renaming the root carries this scope instead of emptying it.
def _memory_spellings() -> dict[str, str]:
    """One durable path per normalisation the model can spell it, keyed by what it exercises."""
    from chemclaw.agent.scratchpad import MEMORY_ROOT

    bare = MEMORY_ROOT.strip("/")
    return {
        "no leading slash": f"{bare}/a.md",
        "a dot component": f"/./{bare}/a.md",
        "a nested key": f"{bare}/sub/b.md",
        "the bare root": f"/{bare}",
        "already canonical": f"{MEMORY_ROOT}a.md",
    }


@pytest.mark.parametrize(("what", "spelling"), sorted(_memory_spellings().items()))
def test_every_spelling_of_a_durable_memory_write_reaches_both_gates(
    what: str, spelling: str
) -> None:
    """A durable per-actor write is gated however the model spelled its path.

    The gate must normalise as the backend does, or spellings such as `memories/a.md` or
    `/./memories/a.md` bypass both the plan gate and the dry-run refusal. `side_effecting_call` is
    asserted too, because it is what both gates call.
    """
    from chemclaw.agent.authz import side_effecting_call, writes_durable_memory

    call = {"file_path": spelling, "content": "x"}
    assert writes_durable_memory("write_file", call), (
        f"{spelling!r} ({what}) is a durable memory write and the gate says it is not"
    )
    assert side_effecting_call("write_file", call), (
        f"{spelling!r} ({what}) reaches neither the plan gate nor the dry-run refusal"
    )


def test_a_turn_local_scratchpad_write_is_still_ungated() -> None:
    """A turn-local scratchpad write is still ungated.

    `/scratch/` dies with the turn; without this the test above is satisfied by `return True`.
    """
    from chemclaw.agent.authz import writes_durable_memory
    from chemclaw.agent.scratchpad import SCRATCH_ROOT

    for spelling in (f"{SCRATCH_ROOT}draft.md", f"{SCRATCH_ROOT.strip('/')}/draft.md"):
        assert not writes_durable_memory("write_file", {"file_path": spelling}), (
            f"{spelling!r} is turn-local and the gate calls it a durable write, so a dry run "
            "refuses the agent its own scratchpad"
        )


def test_upstream_routes_every_spelling_this_gate_calls_durable_to_the_durable_backend() -> None:
    """The upstream behaviour the gate is coupled to, asserted where it is relied on.

    `writes_durable_memory` matches the result of upstream's `validate_path`, which is right only
    while that normalisation and `CompositeBackend._route_for_path` agree. Drives the composite
    `scratchpad_backend` builds, with sentinel backends, so a dependency bump that changes either
    half fails with the spelling that moved.
    """
    from deepagents.backends.composite import CompositeBackend
    from deepagents.backends.utils import validate_path

    from chemclaw.agent.authz import writes_durable_memory
    from chemclaw.agent.scratchpad import MEMORY_ROOT, SCRATCH_ROOT

    durable = object()
    default = object()
    composite = CompositeBackend(
        default=cast(Any, default), routes={MEMORY_ROOT: cast(Any, durable)}
    )

    for what, spelling in _memory_spellings().items():
        assert writes_durable_memory("write_file", {"file_path": spelling}), (
            f"this test's own fixture broke: {spelling!r} ({what}) is not gated at all"
        )
        backend, _key = composite._get_backend_and_key(validate_path(spelling))
        assert backend is durable, (
            f"{spelling!r} ({what}) is gated as a durable write and upstream routes it to the "
            "default backend — the gate and the backend disagree about one path again, which is "
            "the defect `agent/authz.writes_durable_memory` was rewritten to close"
        )

    scratch = f"{SCRATCH_ROOT.strip('/')}/draft.md"
    backend, _key = composite._get_backend_and_key(validate_path(scratch))
    assert backend is default, (
        f"upstream now routes {scratch!r} to the durable backend, so a turn-local scratchpad write "
        "outlives the turn and `agent/authz.writes_durable_memory` calls it ungated"
    )


@pytest.mark.parametrize(
    ("what", "call"),
    [
        ("no file_path at all", {"content": "x"}),
        ("a non-string path", {"file_path": ["/memories/a.md"]}),
        ("a null path", {"file_path": None}),
        ("a traversal validate_path refuses", {"file_path": "../../etc/passwd"}),
        ("a Windows absolute validate_path refuses", {"file_path": "C:/Users/a.md"}),
    ],
)
def test_an_argument_this_gate_cannot_resolve_counts_as_durable(
    what: str, call: dict[str, Any]
) -> None:
    """An unreadable path is the gated case, never the ungated one.

    Both consumers read `False` as permission, so a non-string or a path `validate_path` refuses
    must count as durable; otherwise malformed input bypasses the gate. The cost — a malformed
    scratchpad write refused on a dry run — is accepted.
    """
    from chemclaw.agent.authz import side_effecting_call, writes_durable_memory

    assert writes_durable_memory("write_file", call), (
        f"{what} answered ungated; an argument this gate cannot resolve must be the gated case"
    )
    assert side_effecting_call("write_file", call), f"{what} reaches neither gate"
