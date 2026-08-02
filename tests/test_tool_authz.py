"""Per-tool authorization: the decision and the middleware that enforces it.

Proves `authorize_tool` allows/denies by the turn's ambient roles against `tool_role_gates` under
both defaults, that dev mode is open, and that `enforce_tool_authz` blocks a denied call before the
tool body runs and passes an allowed one through — all offline with fakes, no tenant.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from agent_framework import FunctionInvocationContext

from chemclaw.agent.authz import AuthorizationError, authorize_tool
from chemclaw.agent.identity_context import reset_current_identity, set_current_identity
from chemclaw.agent.tool_authz import (
    announce_tool_failures,
    enforce_tool_authz,
    surface_authorization_denials,
    surface_domain_errors,
)
from chemclaw.agent.turn_signals import Signal, ToolFailureSignal, begin_turn, drain, end_turn
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError


def _enforced(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Turn Entra enforcement on (the gate is a no-op otherwise) plus any config overrides."""
    monkeypatch.setattr(settings, "entra_required", True)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)


def test_dev_mode_gate_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, every tool is callable (local dev, no tenant)."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "tool_authz_default", "deny")  # ignored in dev
    authorize_tool("compute_dft_energy")  # does not raise


def test_allow_default_lets_ungated_tools_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool with no gate entry is allowed under the default 'allow' policy (today's behavior)."""
    _enforced(monkeypatch, tool_role_gates={}, tool_authz_default="allow")
    authorize_tool("find_notes")  # ungated → allowed


def test_gated_tool_requires_a_permitted_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gated tool is allowed for a role-holder and denied for a user lacking the role."""
    _enforced(monkeypatch, tool_role_gates={"compute_dft_energy": ["process-chemist"]})

    ok = set_current_identity("u-1", frozenset({"process-chemist"}))
    try:
        authorize_tool("compute_dft_energy")  # holds the role → allowed
    finally:
        reset_current_identity(ok)

    denied = set_current_identity("u-2", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            authorize_tool("compute_dft_energy")
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
        with pytest.raises(AuthorizationError, match="not authorized to use compute_dft_energy"):
            authorize_tool("compute_dft_energy")
        with pytest.raises(AuthorizationError):
            authorize_tool("propose_knowledge_note")
        authorize_tool("find_notes")  # read tools stay open under 'allow'
    finally:
        reset_current_identity(denied)

    ok = set_current_identity("u-7", frozenset({"process-chemist"}))
    try:
        authorize_tool("compute_dft_energy")  # privileged role → allowed
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
        tool_role_gates={"compute_dft_energy": ["reader"]},
        entra_privileged_roles="process-chemist",
    )
    token = set_current_identity("u-9", frozenset({"reader"}))
    try:
        authorize_tool("compute_dft_energy")  # operator opened it to 'reader' → allowed
    finally:
        reset_current_identity(token)


def test_dev_mode_leaves_write_tools_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """With enforcement off, the built-in write gates are no-ops (local dev unchanged)."""
    monkeypatch.setattr(settings, "entra_required", False)
    authorize_tool("compute_dft_energy")
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
            authorize_tool("compute_dft_energy")  # not in the allowlist → denied
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
            authorize_tool("compute_dft_energy")
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
    _enforced(monkeypatch, tool_role_gates={"compute_dft_energy": ["process-chemist"]})
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    token = set_current_identity("u-4", frozenset({"reader"}))
    try:
        with pytest.raises(AuthorizationError):
            _drive(_ctx("compute_dft_energy"), _body)
    finally:
        reset_current_identity(token)
    assert ran is False  # the tool body was never reached


def test_middleware_passes_an_authorized_call_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An authorized tool runs unchanged through the middleware."""
    _enforced(monkeypatch, tool_role_gates={"compute_dft_energy": ["process-chemist"]})
    ran = False

    async def _body() -> None:
        nonlocal ran
        ran = True

    token = set_current_identity("u-5", frozenset({"process-chemist"}))
    try:
        _drive(_ctx("compute_dft_energy"), _body)
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
        raise AuthorizationError(
            "u-9 lacks a privileged role for the write tool compute_dft_energy"
        )

    ctx = _ctx("compute_dft_energy")
    _drive_surfacing(ctx, _denied)  # must not raise
    assert ctx.result == (
        "Refused: u-9 lacks a privileged role for the write tool compute_dft_energy"
    )


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
    chemclaw's own always-safe "bad input" contract (`chemclaw.core.errors`), so its message is safe
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
        ("compute_dft_energy", "write_gate"),
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


def _drive_announcing(
    ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
) -> list[Signal]:
    """Run `announce_tool_failures` inside a turn and return the signals it left behind."""

    async def _run() -> list[Signal]:
        token = begin_turn()
        try:
            with contextlib.suppress(Exception):
                await announce_tool_failures(ctx, call_next)
            return list(drain())
        finally:
            end_turn(token)

    return asyncio.run(_run())


def test_a_failing_tool_is_announced_to_the_turn() -> None:
    """The chemist's transcript learns the step failed; until this, only the log and audit did.

    The live shape that motivated it: a job launcher raised on every attempt, MAF stopped the
    tool loop after three consecutive errors, and the turn ended mid-sentence with no answer and
    no error event — nothing anywhere in the stream said a tool had failed (D-138).
    """

    async def _boom() -> None:
        raise AttributeError("'dict' object has no attribute 'model_dump'")

    (signal,) = _drive_announcing(_ctx("compute_reaction_energy"), _boom)
    assert isinstance(signal, ToolFailureSignal)
    assert signal.tool == "compute_reaction_energy"
    assert signal.message.startswith("AttributeError: 'dict' object has no attribute")


def test_the_failing_exception_still_propagates_untouched() -> None:
    """Announcing is observation: audit and the two converters must see exactly what they did."""

    async def _boom() -> None:
        raise ValueError("unrelated failure")

    async def _run() -> None:
        with pytest.raises(ValueError, match="unrelated failure"):
            await announce_tool_failures(_ctx("predict_pka"), _boom)

    asyncio.run(_run())


def test_a_successful_call_announces_nothing() -> None:
    """No signal on the happy path — the trace must not gain an entry per working tool."""

    async def _ok() -> None:
        return None

    assert _drive_announcing(_ctx("predict_pka"), _ok) == []


def test_a_long_failure_message_is_truncated_before_it_reaches_the_stream() -> None:
    """An unexpected exception's text is not written to be read, and must not flood the trace."""

    async def _boom() -> None:
        raise RuntimeError("x" * 5000)

    (signal,) = _drive_announcing(_ctx("predict_pka"), _boom)
    assert isinstance(signal, ToolFailureSignal)
    assert len(signal.message) <= 300


# --- infrastructure and calculator refusals must reach the model, not just the trace ----


def test_a_calculator_domain_refusal_reaches_the_model_verbatim() -> None:
    """`predict_pka`'s real refusal, driven through the real middleware, arrives as its message.

    Deliberately raised by the production code path rather than by a hand-thrown error: the defect
    was that these sites raised a *bare* `ValueError`, and `ChemclawError` subclasses `ValueError`,
    so `except ChemclawError` could not catch one — the inheritance runs the wrong way. A test that
    throws `CalculationDomainError` itself would pass before the fix and prove nothing.

    Measured consequence, 2026-08-02 live run: the aliphatic-amine explanation — which names the
    Spearman -0.17 correlation and tells the chemist to measure instead — reached the model as
    "Error: Function failed.", and the answer then guessed the reason and stated the guess as fact.
    """
    from chemclaw.science.calc.pka import PkaInput, predict_pka

    async def _refuse() -> None:
        # Ethane: nothing acidic to lose, no nitrogen to protonate.
        predict_pka(PkaInput(smiles="CC"))

    ctx = _ctx("predict_pka")
    _drive_domain_errors(ctx, _refuse)  # must not raise
    assert isinstance(ctx.result, str)
    assert ctx.result.startswith("Error: ")
    assert "nothing to" in ctx.result, ctx.result


def test_an_unreachable_durable_backend_says_nothing_was_started() -> None:
    """A broker outage must reach the model as an outage, and must say nothing is queued.

    A chemist who believes a job may be running will wait for it. The live run's failure was worse
    than silence: the model read the opaque error as bad input, retried three SMILES variants, and
    ended the turn mid-sentence.
    """
    from chemclaw.connectors.jobs import ConnectorJobError

    async def _unreachable() -> None:
        raise ConnectorJobError(
            "the durable execution backend is unreachable, so the 'compute_reaction_energy' job "
            "was not started and nothing is queued (RPCError)."
        )

    ctx = _ctx("compute_reaction_energy")
    _drive_domain_errors(ctx, _unreachable)
    assert isinstance(ctx.result, str)
    assert "not started" in ctx.result and "nothing is queued" in ctx.result


def test_a_pr_gate_git_failure_reaches_the_model() -> None:
    """`GitSubmitError` must surface, because its silence made the gate publish ungated.

    Told only "Error: Function failed.", the model retried five times permuting its arguments and
    then printed the unreviewed document into the chat as a fallback.
    """
    from chemclaw.kg.git_submitter import GitSubmitError

    async def _git_failed() -> None:
        raise GitSubmitError("note_repo_dir has no 'origin' remote; nothing was submitted")

    ctx = _ctx("propose_knowledge_note")
    _drive_domain_errors(ctx, _git_failed)
    assert isinstance(ctx.result, str)
    assert "no 'origin' remote" in ctx.result
