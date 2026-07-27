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
from agents.tool_authz import (
    enforce_tool_authz,
    surface_authorization_denials,
    surface_domain_errors,
)
from chemclaw.config import settings
from chemclaw.errors import ChemclawError


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
        with pytest.raises(AuthorizationError, match="not authorized to use submit_qm_job"):
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


def test_deny_default_refuses_write_tools_even_for_privileged_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under 'deny', an unlisted write tool is refused even for a privileged-role holder.

    The built-in write gate only *narrows* the 'allow' default; it must never widen
    'deny' — an empty `tool_role_gates` under 'deny' is documented as blocking ALL
    tools, and privileged roles are not an allowlist entry.
    """
    _enforced(
        monkeypatch,
        tool_authz_default="deny",
        tool_role_gates={},
        entra_privileged_roles="process-chemist",
    )
    token = set_current_identity("u-10", frozenset({"process-chemist"}))
    try:
        with pytest.raises(AuthorizationError, match="not authorized to use"):
            authorize_tool("submit_qm_job")
        with pytest.raises(AuthorizationError, match="not authorized to use"):
            authorize_tool("propose_knowledge_note")
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


def _drive_surfacing(
    ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
) -> None:
    """Run `surface_authorization_denials` over a stand-in context to completion."""

    async def _run() -> None:
        await surface_authorization_denials(ctx, call_next)

    asyncio.run(_run())


def test_surfacing_converts_a_denial_into_the_tool_s_own_result() -> None:
    """A denial becomes the call's own safe, readable result — not a re-raised exception.

    Without this, MAF's function-invocation executor collapses *any* escaping exception into
    the same opaque "Error: Function failed." with zero explanation reaching the model
    (`include_detailed_errors` defaults off) — so a real chemist question ("why didn't that
    run?") got answered with an invented guess ("a temporary service issue") instead of the
    true, safe reason.
    """

    async def _denied() -> None:
        raise AuthorizationError("u-9 lacks a privileged role for the write tool submit_qm_job")

    ctx = _ctx("submit_qm_job")
    _drive_surfacing(ctx, _denied)  # must not raise
    assert ctx.result == "Refused: u-9 lacks a privileged role for the write tool submit_qm_job"


def test_surfacing_leaves_other_exceptions_untouched() -> None:
    """Only `AuthorizationError` is caught — an unrelated failure still propagates as-is.

    Any other exception (a bug, a database error) must keep falling through to MAF's generic,
    safe-by-omission handling; only chemclaw's own, deliberately-worded denial message is
    known-safe enough to surface verbatim.
    """

    async def _boom() -> None:
        raise ValueError("unrelated failure")

    with pytest.raises(ValueError, match="unrelated failure"):
        _drive_surfacing(_ctx("predict_pka"), _boom)


def test_surfacing_passes_a_successful_call_through_unchanged() -> None:
    """A call that succeeds is unaffected — no result override, no swallowed exception."""
    ctx = _ctx("predict_pka")
    ctx.result = None

    async def _ok() -> None:
        ctx.result = "6.51"

    _drive_surfacing(ctx, _ok)
    assert ctx.result == "6.51"


def _drive_domain_errors(
    ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
) -> None:
    """Run `surface_domain_errors` over a stand-in context to completion."""

    async def _run() -> None:
        await surface_domain_errors(ctx, call_next)

    asyncio.run(_run())


def test_domain_errors_convert_a_chemclaw_error_into_the_tool_s_own_result() -> None:
    """A `ChemclawError` becomes the call's own safe, readable result — not a re-raised exception.

    Regression guard for a live e2e finding: `expand_note` citing a reaction note still pending
    PR-gate review failed with MAF's opaque "Error: Function failed.", so the model could not
    tell "pending review" apart from "typo'd id" and could only guess. `ChemclawError` is
    chemclaw's own always-safe "bad input" contract (`chemclaw.errors`), so its message is safe
    to surface verbatim, exactly like `AuthorizationError`.
    """

    async def _not_found() -> None:
        raise ChemclawError("no note with id 'reaction-ghost'")

    ctx = _ctx("expand_note")
    _drive_domain_errors(ctx, _not_found)  # must not raise
    assert ctx.result == "Error: no note with id 'reaction-ghost'"


def test_domain_errors_leave_other_exceptions_untouched() -> None:
    """Only `ChemclawError` is caught — an unrelated failure still propagates as-is."""

    async def _boom() -> None:
        raise RuntimeError("unrelated failure")

    with pytest.raises(RuntimeError, match="unrelated failure"):
        _drive_domain_errors(_ctx("predict_pka"), _boom)


def test_domain_errors_pass_a_successful_call_through_unchanged() -> None:
    """A call that succeeds is unaffected — no result override, no swallowed exception."""
    ctx = _ctx("predict_pka")
    ctx.result = None

    async def _ok() -> None:
        ctx.result = "6.51"

    _drive_domain_errors(ctx, _ok)
    assert ctx.result == "6.51"


# --- the refusal wording the chemist actually reads ------------------------------------


def _denial_message(tool: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Return the message `authorize_tool` refuses `tool` with, for the configured gate."""
    monkeypatch.setattr(settings, "entra_required", True)
    token = set_current_identity("u-7", frozenset({"chemist"}))
    try:
        with pytest.raises(AuthorizationError) as exc_info:
            authorize_tool(tool)
    finally:
        reset_current_identity(token)
    return str(exc_info.value)


@pytest.mark.parametrize(
    ("tool", "configure"),
    [
        ("predict_pka", "explicit_gate"),
        ("predict_pka", "deny_default"),
        ("submit_qm_job", "write_gate"),
    ],
)
def test_every_denial_reads_as_an_access_decision(
    tool: str, configure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three refusal paths name the user, name the tool, and say it is an access decision.

    They used to diverge, and the divergence reached the chemist: the deny-default message was
    phrased for whoever edits the config ("not in the tool allowlist"), so the model relayed a
    denial as "not currently available… a configuration issue" — sending a chemist to report a
    bug rather than to request access. The built-in write gate said "lacks a privileged role" and
    narrated correctly, which is why only the write tools ever explained themselves. One shape for
    all three, so which gate fired cannot change whether the answer is intelligible.
    """
    if configure == "explicit_gate":
        monkeypatch.setattr(settings, "tool_role_gates", {tool: ["reviewer"]})
    elif configure == "deny_default":
        monkeypatch.setattr(settings, "tool_authz_default", "deny")
    else:
        monkeypatch.setattr(settings, "entra_privileged_roles", "lead")

    message = _denial_message(tool, monkeypatch)

    assert message.startswith("u-7 is not authorized to use ")  # who, and that it is authorization
    assert tool in message  # which tool, so the chemist can ask for that access specifically
    assert ":" in message  # ...followed by the reason
    # None of the words that previously made this read as a malfunction rather than a decision.
    lowered = message.lower()
    for misleading in ("allowlist", "unavailable", "not working", "temporarily", "config"):
        assert misleading not in lowered, f"{misleading!r} reads as a fault, not an access decision"


def test_an_unauthenticated_user_is_named_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no identity in context the message says so, rather than naming an empty actor."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_authz_default", "deny")
    with pytest.raises(AuthorizationError, match="an unauthenticated user is not authorized"):
        authorize_tool("predict_pka")
