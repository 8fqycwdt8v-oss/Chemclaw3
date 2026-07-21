"""The single authorization gate for expensive triggers (plan Phase F4-T5), offline.

Proves `authorize_trigger` allows/denies by the turn's ambient roles per config, that the audit
trail attributes to the real ambient actor, and that `submit_qm_job` both authorizes and stamps the
requesting user — all with fakes, no Temporal or tenant.
"""

import asyncio

import pytest

import agents.qm_tools as qm_tools
from agents.authz import AuthorizationError, authorize_trigger, require_actor
from agents.identity_context import reset_current_identity, set_current_identity
from chemclaw.config import settings


def _privileged_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "submit_qm_job")
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")


def test_dev_mode_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, every trigger is allowed (local dev, no tenant)."""
    monkeypatch.setattr(settings, "entra_required", False)
    authorize_trigger("submit_qm_job")  # does not raise


def test_non_expensive_action_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An action not declared expensive is allowed even under enforcement."""
    _privileged_env(monkeypatch)
    authorize_trigger("find_notes")  # not in the expensive set → allowed


def test_privileged_role_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user holding a privileged role may trigger the expensive action."""
    _privileged_env(monkeypatch)
    token = set_current_identity("u-1", frozenset({"compute"}))
    try:
        authorize_trigger("submit_qm_job")  # does not raise
    finally:
        reset_current_identity(token)


def test_missing_role_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user without a privileged role cannot trigger the expensive action."""
    _privileged_env(monkeypatch)
    token = set_current_identity("u-2", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            authorize_trigger("submit_qm_job")
    finally:
        reset_current_identity(token)


def test_no_user_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under enforcement, an expensive action with no authenticated user is rejected."""
    _privileged_env(monkeypatch)
    with pytest.raises(AuthorizationError):
        authorize_trigger("submit_qm_job")


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


class _FakeHandle:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _CapturingClient:
    def __init__(self) -> None:
        self.started: list[object] = []

    async def start_workflow(
        self, _run: object, job: object, *, id: str, **_: object
    ) -> _FakeHandle:
        self.started.append(job)
        return _FakeHandle(id)


def test_submit_qm_job_denied_without_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """`submit_qm_job` refuses an unauthorized user before touching Temporal."""
    _privileged_env(monkeypatch)
    client = _CapturingClient()
    monkeypatch.setattr(qm_tools, "connect", lambda: _ready(client))
    token = set_current_identity("u-3", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            asyncio.run(qm_tools.submit_qm_job("CCO", "B3LYP", "def2-SVP"))
    finally:
        reset_current_identity(token)
    assert client.started == []  # no workflow was started


def test_submit_qm_job_stamps_requested_by(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authorized submit records the requesting Entra oid on the durable job."""
    _privileged_env(monkeypatch)
    client = _CapturingClient()
    monkeypatch.setattr(qm_tools, "connect", lambda: _ready(client))
    token = set_current_identity("u-4", frozenset({"compute"}))
    try:
        asyncio.run(qm_tools.submit_qm_job("CCO", "B3LYP", "def2-SVP"))
    finally:
        reset_current_identity(token)
    assert client.started and client.started[0].requested_by == "u-4"


def test_submit_qm_job_rejects_absent_user_even_when_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject-if-absent is independent of the role gate: no user → no job, even if unguarded."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", "")  # authorize_trigger is a no-op
    client = _CapturingClient()
    monkeypatch.setattr(qm_tools, "connect", lambda: _ready(client))
    with pytest.raises(AuthorizationError):
        asyncio.run(qm_tools.submit_qm_job("CCO", "B3LYP", "def2-SVP"))
    assert client.started == []  # require_actor refused before Temporal


async def _ready(client: _CapturingClient) -> _CapturingClient:
    return client
