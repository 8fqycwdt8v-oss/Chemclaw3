"""The single authorization gate for expensive triggers (plan Phase F4-T5), offline.

Proves `authorize_trigger` allows/denies by the turn's ambient roles per config, and that the audit
trail attributes to the real ambient actor — all with fakes, no Temporal or tenant.

The *launcher* half — that an expensive job authorizes and stamps the requesting user before any
durable work — moved with the launchers themselves: every durable capability is a declared
connector job now (D-118), so `tests/test_connector_jobs.py` proves it once for all of them
instead of once per hand-written tool.
"""

import pytest

from chemclaw.agent.authz import AuthorizationError, authorize_trigger, require_actor
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
