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
    authorize_trigger,
    require_actor,
)
from chemclaw.agent.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.config import settings


def _privileged_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "compute_dft_energy")
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")


def test_dev_mode_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, every trigger is allowed (local dev, no tenant)."""
    monkeypatch.setattr(settings, "entra_required", False)
    authorize_trigger("compute_dft_energy")  # does not raise


def test_non_expensive_action_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action not declared expensive is allowed even under enforcement."""
    _privileged_env(monkeypatch)
    authorize_trigger("find_notes")  # not in the expensive set → allowed


def test_privileged_role_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user holding a privileged role may trigger the expensive action."""
    _privileged_env(monkeypatch)
    token = set_current_identity("u-1", frozenset({"compute"}))
    try:
        authorize_trigger("compute_dft_energy")  # does not raise
    finally:
        reset_current_identity(token)


def test_missing_role_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user without a privileged role cannot trigger the expensive action."""
    _privileged_env(monkeypatch)
    token = set_current_identity("u-2", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            authorize_trigger("compute_dft_energy")
    finally:
        reset_current_identity(token)


def test_no_user_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under enforcement, an expensive action with no authenticated user is rejected."""
    _privileged_env(monkeypatch)
    with pytest.raises(AuthorizationError):
        authorize_trigger("compute_dft_energy")


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


# --- the write/read classification, held to the registry it describes (D-165) ------------------


def test_every_advertised_tool_is_classified_write_or_read() -> None:
    """A new tool must be classified, or this fails — the gate cannot infer what a tool does.

    `chemclaw.agent.plan_gate.gated_tools` is what the harness's plan gate refuses under an
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
    from chemclaw.agent.chemclaw_agent import build_agent
    from chemclaw.agent.plan_gate import gated_tools
    from chemclaw.agent.tool_registry import registered_tool_names

    build_agent(chat_client=object())
    advertised = set(registered_tool_names())
    classified = gated_tools() | READ_ONLY_TOOLS
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

    The two are separate on purpose — membership of `DEFAULT_WRITE_TOOL_GATES` costs an
    unconfigured deployment access to a tool, so it is not widened lightly — but a tool that closes
    by default under RBAC and is *not* considered state-changing by the plan gate would be an
    outright contradiction.
    """
    assert DEFAULT_WRITE_TOOL_GATES <= STATE_CHANGING_TOOLS
