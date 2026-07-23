"""Per-tool authorization: the decision (agents.authz) and the middleware (agents.tool_authz).

Proves `authorize_tool` allows/denies by the turn's ambient roles against `tool_role_gates` under
both defaults, that dev mode is open, and that `enforce_tool_authz` blocks a denied call before the
tool body runs and passes an allowed one through — all offline with fakes, no tenant.
"""

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from agent_framework import FunctionInvocationContext

from agents.authz import AuthorizationError, authorize_tool
from agents.identity_context import reset_current_identity, set_current_identity
from agents.tool_authz import enforce_tool_authz
from chemclaw.config import settings


def _enforced(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Turn Entra enforcement on (the gate is a no-op otherwise) plus any config overrides."""
    monkeypatch.setattr(settings, "entra_required", True)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)


def test_dev_mode_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, every tool is callable (local dev, no tenant)."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "tool_authz_default", "deny")  # ignored in dev
    authorize_tool("submit_qm_job")  # does not raise


def test_allow_default_lets_ungated_tools_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool with no gate entry is allowed under the default 'allow' policy (today's behavior)."""
    _enforced(monkeypatch, tool_role_gates={}, tool_authz_default="allow")
    authorize_tool("find_notes")  # ungated → allowed


def test_gated_tool_requires_a_permitted_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gated tool is allowed for a role-holder and denied for a user lacking the role."""
    _enforced(monkeypatch, tool_role_gates={"submit_qm_job": ["process-chemist"]})

    ok = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        authorize_tool("submit_qm_job")  # holds the role → allowed
    finally:
        reset_current_identity(ok)

    denied = set_current_identity("u-2", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            authorize_tool("submit_qm_job")
    finally:
        reset_current_identity(denied)


def test_write_tools_are_gated_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured write tool requires a privileged role even under the 'allow' default.

    The built-in `DEFAULT_WRITE_TOOL_GATES` closes job launchers and state-mutating tools
    out of the box: only `entra_privileged_roles` holders may call them until an operator
    sets an explicit gate.
    """
    _enforced(
        monkeypatch,
        tool_role_gates={},
        tool_authz_default="allow",
        entra_privileged_roles="process-chemist",
    )

    denied = set_current_identity("u-6", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError, match="write tool submit_qm_job"):
            authorize_tool("submit_qm_job")
        with pytest.raises(AuthorizationError):
            authorize_tool("propose_knowledge_note")
        authorize_tool("find_notes")  # read tools stay open under 'allow'
    finally:
        reset_current_identity(denied)

    ok = set_current_identity("u-7", frozenset({"process-chemist"}))
    try:
        authorize_tool("submit_qm_job")  # privileged role → allowed
    finally:
        reset_current_identity(ok)


def test_default_write_gate_fails_closed_without_privileged_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no `entra_privileged_roles` configured, a default-gated write tool is denied.

    An empty required set means 'no role needed' for operator gates, but the built-in write
    gate must not silently open on an unconfigured deployment.
    """
    _enforced(monkeypatch, tool_role_gates={}, entra_privileged_roles="")
    token = set_current_identity("u-8", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            authorize_tool("record_confirmed_answer")
    finally:
        reset_current_identity(token)


def test_explicit_operator_gate_overrides_the_default_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `tool_role_gates` entry for a write tool replaces the built-in privileged-role gate."""
    _enforced(
        monkeypatch,
        tool_role_gates={"submit_qm_job": ["reader"]},
        entra_privileged_roles="process-chemist",
    )
    token = set_current_identity("u-9", frozenset({"reader"}))
    try:
        authorize_tool("submit_qm_job")  # operator opened it to 'reader' → allowed
    finally:
        reset_current_identity(token)


def test_dev_mode_leaves_write_tools_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, the built-in write gates are no-ops (local dev unchanged)."""
    monkeypatch.setattr(settings, "entra_required", False)
    authorize_tool("submit_qm_job")
    authorize_tool("propose_knowledge_note")
    authorize_tool("record_confirmed_answer")


def test_deny_default_blocks_ungated_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the 'deny' default an ungated tool is refused; a gated one still works by role."""
    _enforced(
        monkeypatch,
        tool_authz_default="deny",
        tool_role_gates={"find_notes": ["reader"]},
    )
    token = set_current_identity("u-3", frozenset({"reader"}))
    try:
        authorize_tool("find_notes")  # gated + role held → allowed
        with pytest.raises(AuthorizationError):
            authorize_tool("submit_qm_job")  # not in the allowlist → denied
    finally:
        reset_current_identity(token)


def _ctx(name: str) -> FunctionInvocationContext:
    """A minimal stand-in exposing the one field the middleware reads."""
    return cast(FunctionInvocationContext, SimpleNamespace(function=SimpleNamespace(name=name)))


def _drive(ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
    """Run the authz middleware over a stand-in context to completion."""

    async def _run() -> None:
        await enforce_tool_authz(ctx, call_next)

    asyncio.run(_run())


def test_middleware_blocks_a_denied_call_before_the_tool_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`enforce_tool_authz` raises for an unauthorized tool and never invokes the tool body."""
    _enforced(monkeypatch, tool_role_gates={"submit_qm_job": ["process-chemist"]})
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    token = set_current_identity("u-4", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            _drive(_ctx("submit_qm_job"), _body)
    finally:
        reset_current_identity(token)
    assert ran is False  # the tool body was never reached


def test_middleware_passes_an_authorized_call_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authorized tool runs unchanged through the middleware."""
    _enforced(monkeypatch, tool_role_gates={"submit_qm_job": ["process-chemist"]})
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    token = set_current_identity("u-5", frozenset({"process-chemist"}))
    try:
        _drive(_ctx("submit_qm_job"), _body)
    finally:
        reset_current_identity(token)
    assert ran is True
