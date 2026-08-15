"""Per-tool authorization: the decision and the middleware that enforces it.

Proves `authorize_tool` allows/denies by the turn's ambient roles against `tool_role_gates` under
both defaults, that dev mode is open, and that `enforce_tool_authz` blocks a denied call before
the tool body runs and passes an allowed one through — all offline with fakes, no tenant.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.authz import AuthorizationError, authorize_tool
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.tool_authz import (
    announce_tool_failures,
    enforce_tool_authz,
    surface_authorization_denials,
    surface_domain_errors,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.turn_signals import _KEY as _SIGNAL_KEY
from chemclaw.core.turn_signals import Signal, ToolFailureSignal
from tests.fakes_langgraph import ScriptedChatModel
from tests.middleware import run_middleware, tool_request
from tests.signals import collect_signals


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


def _ctx(name: str) -> Any:
    """The call as the middleware reads it, with a slot for what it produced.

    A `ToolCallRequest` plus a mutable `result` the assertions below read. The MAF halves these
    replaced *wrote* their result onto the invocation context, so the tests were written against
    that shape; a `wrap_tool_call` middleware returns a `ToolMessage` instead. `_drive_surfacing`
    stores what came back on the request, which keeps the assertions about the *decision* rather
    than about how the framework hands a result along.
    """
    request = tool_request(name)
    object.__setattr__(request, "result", None)
    return request


def _drive(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run the authz middleware over one call to completion."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    asyncio.run(run_middleware(enforce_tool_authz, ctx, _handler))


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


def _drive_surfacing(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run `surface_authorization_denials` over one call, storing what it produced on `ctx`."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    returned = asyncio.run(run_middleware(surface_authorization_denials, ctx, _handler))
    object.__setattr__(ctx, "result", getattr(returned, "content", returned))


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

    async def _ok() -> str:
        return "6.51"

    _drive_surfacing(ctx, _ok)
    assert ctx.result == "6.51"


def _drive_domain_errors(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run `surface_domain_errors` over one call, storing what it produced on `ctx`."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    returned = asyncio.run(run_middleware(surface_domain_errors, ctx, _handler))
    object.__setattr__(ctx, "result", getattr(returned, "content", returned))


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


def test_an_unclassified_failure_becomes_a_result_rather_than_ending_the_turn() -> None:
    """A failed tool is a recoverable step. It stopped being one, and nothing said so.

    `announce_tool_failures` records and re-raises, neither converter caught anything outside the
    two safe families, and `ToolNode`'s default handler re-raises what it is given — so a `KeyError`
    from a parser or a driver's `TimeoutError` escaped the graph and killed the whole turn. The
    chemist lost the answer, the tokens, and every other tool the turn had already run, for one
    failed step. The framework this replaced collapsed any tool exception into a result, so this is
    a regression rather than a decision.
    """
    ctx = _ctx("predict_pka")

    async def _boom() -> None:
        raise RuntimeError("psycopg: could not connect to host db-7.internal user=chemclaw")

    _drive_domain_errors(ctx, _boom)

    assert ctx.result, "the turn was ended by a tool failure instead of continuing"
    # And the model is told nothing about the exception: an unclassified fault's text is not vetted
    # for a model to read, and this one carries a hostname and a role name.
    assert "db-7.internal" not in str(ctx.result)
    assert "chemclaw" not in str(ctx.result)
    assert "failed unexpectedly" in str(ctx.result)


def test_a_cancellation_is_never_converted_into_a_tool_result() -> None:
    """`CancelledError` is how a disconnect and the turn deadline arrive.

    Converting one into a result would swallow the cancellation and leave the turn running after
    the client is gone — which is why the catch is `Exception` and not `BaseException`.
    """

    async def _cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        _drive_domain_errors(_ctx("predict_pka"), _cancelled)


def test_domain_errors_pass_a_successful_call_through_unchanged() -> None:
    """A call that succeeds is unaffected — no result override, no swallowed exception."""
    ctx = _ctx("predict_pka")

    async def _ok() -> str:
        return "6.51"

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


def _drive_announcing(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> list[Signal]:
    """Run `announce_tool_failures` inside a turn and return the signals it left behind."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    async def _announce() -> None:
        with contextlib.suppress(Exception):
            await run_middleware(announce_tool_failures, ctx, _handler)

    async def _run() -> list[Signal]:
        _returned, signals = await collect_signals(_announce)
        return signals

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

    async def _handler(_request: Any) -> Any:
        return await _boom()

    async def _run() -> None:
        with pytest.raises(ValueError, match="unrelated failure"):
            await run_middleware(announce_tool_failures, _ctx("predict_pka"), _handler)

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


def test_an_unreachable_durable_backend_says_nothing_was_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker outage must reach the model as an outage, and must say nothing was queued.

    A chemist who believes a job may be running will wait for it. The live run's failure was worse
    than silence: told only "Error: Function failed.", the model read the outage as bad input,
    retried three SMILES variants, and on another turn **wrote a whole development report by hand**
    and presented it as PR-gated.

    Driven through the real `connect()` against a real closed port, not a hand-thrown error:
    `SubsystemUnavailableError` is deliberately *not* a `ChemclawError`, so a middleware that only
    caught that hierarchy would still drop this on the floor — and the point of the test is that
    the second type is caught, which a fabricated instance of the right class would not prove.
    """
    import socket

    from chemclaw.core import temporal_client

    with socket.socket() as probe:  # a port nothing is listening on
        probe.bind(("127.0.0.1", 0))
        closed = f"127.0.0.1:{probe.getsockname()[1]}"
    monkeypatch.setattr(settings, "temporal_address", closed)
    monkeypatch.setattr(temporal_client, "_CLIENT", None)
    monkeypatch.setattr(temporal_client, "_CONNECT_LOCK", asyncio.Lock())

    async def _launch() -> None:
        await temporal_client.connect()

    ctx = _ctx("start_optimization_campaign")
    _drive_domain_errors(ctx, _launch)  # must not raise
    assert isinstance(ctx.result, str)
    assert ctx.result.startswith("Error: ")
    assert "Temporal" in ctx.result and "nothing was queued" in ctx.result


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


# --- the third way a tool call fails: it returns instead of raising ---------------------


@contextlib.asynccontextmanager
async def _connector_tools() -> AsyncIterator[dict[str, Any]]:
    """The real MCP tools of a two-tool server, keyed by name, over an in-memory session.

    Real components on both sides of the boundary — a real `FastMCP` server, the real MCP client
    session, and the real `load_mcp_tools` conversion — because the whole premise of these tests is
    a shape *upstream* produces: a tool that fails by returning `ToolMessage(status="error")`
    instead of raising. A hand-built `ToolMessage` would assert that the middleware does what the
    test author already believed, which is exactly the class of proof this repository has been
    burned by. Only the socket is dropped, which changes nothing about the message.
    """
    server = FastMCP("refusals")

    @server.tool()
    async def refuse_smiles(smiles: str) -> str:
        """Refuse the way a connector tool refuses: raise *over there*, out of core's reach."""
        raise ChemclawError(f"{smiles} has an unclosed ring")

    @server.tool()
    async def echo_smiles(smiles: str) -> str:
        """Succeed, so the mirror case has something that must be left alone."""
        return f"echoed {smiles}"

    # `_mcp_server` is the low-level server `FastMCP` wraps; the in-memory transport takes that
    # rather than the FastAPI app `connectors/server.py` builds around it for a deployment.
    async with create_connected_server_and_client_session(server._mcp_server) as session:
        yield {tool.name: tool for tool in await load_mcp_tools(session)}


async def _through_domain_errors(tool: Any, smiles: str) -> Any:
    """Call `tool` for real inside `surface_domain_errors`, and return what the model would read."""

    async def _handler(request: Any) -> Any:
        return await tool.ainvoke(request.tool_call)

    request = tool_request(tool.name, {"smiles": smiles})
    return await run_middleware(surface_domain_errors, request, _handler)


def test_a_connector_refusal_reaches_the_model_without_the_retry_flag() -> None:
    """BACKLOG:317 — the policy `_refusal_message` states, applied to the kind that returns.

    `status="error"` reaches Anthropic as `is_error` on the tool_result block, which invites the
    retry a worded refusal exists to prevent. Both in-process kinds raise, so both converters
    answer with a `_refusal_message` that carries no such flag; the MCP kind returns, so nothing
    converted it and the connector — where most domain refusals are actually diagnosed — was the
    one path sending it.

    The premise is asserted first, on the untouched tool, so this test fails loudly rather than
    vacuously the day the adapter stops flagging a failed call.
    """

    async def _go() -> None:
        async with _connector_tools() as tools:
            raw = await tools["refuse_smiles"].ainvoke(
                {
                    "name": "refuse_smiles",
                    "args": {"smiles": "c1ccccc"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            )
            assert raw.status == "error", (
                "the adapter no longer returns a flagged failure; this whole conversion is moot"
            )

            answered = await _through_domain_errors(tools["refuse_smiles"], "c1ccccc")
            assert answered.status == "success", (
                "a connector refusal still reaches the provider as a retryable error"
            )
            # The server's own sentence, verbatim — a refusal that arrives without its reason is
            # no better than the flag it was carrying.
            assert "c1ccccc has an unclosed ring" in answered.text
            # And it still answers the call it was made for: an assistant tool_use block with no
            # matching tool_result is a malformed exchange the provider rejects outright.
            assert answered.tool_call_id == "call-1"

    asyncio.run(_go())


def test_a_working_connector_tool_is_handed_back_untouched() -> None:
    """The mirror: nothing is rewritten for a call that worked.

    A predicate that fired on any returned `ToolMessage` rather than on a failed one would silently
    rewrite every successful connector result, which is the same defect mirrored.
    """

    async def _go() -> None:
        async with _connector_tools() as tools:
            answered = await _through_domain_errors(tools["echo_smiles"], "CCO")
            assert answered.status == "success"
            assert "echoed CCO" in answered.text

    asyncio.run(_go())


class _RecordingSink:
    """An audit sink that keeps what it was given, so a turn's trail can be asserted on."""

    def __init__(self) -> None:
        """Start with an empty trail."""
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        """Keep the event."""
        self.events.append(event)


def test_the_trail_and_the_transcript_still_see_the_failure_the_model_is_spared() -> None:
    """The three readers must not cancel each other out — one real turn, all three checked.

    The flag is cleared *outside* the audit middleware and the announcer, which is the whole reason
    it is cleared there. Clearing it any lower — at the MCP seam, where `langchain_mcp_adapters`
    offers a `ToolCallInterceptor` that could rewrite the `CallToolResult` before it is ever
    converted — would leave both of them reading a success, re-opening BACKLOG:309 in the act of
    closing BACKLOG:317. Only a composed run can show that, so this drives the real compiled graph
    rather than a hand-nested chain, and reads the chemist's signals off a real stream writer.
    """
    sink = _RecordingSink()

    async def _go() -> tuple[Any, list[Signal]]:
        async with _connector_tools() as tools:
            graph = build_langgraph_agent(
                model=ScriptedChatModel(
                    [{"name": "refuse_smiles", "args": {"smiles": "c1ccccc"}}, "done"]
                ),
                audit_sink=sink,
                connectors=[tools["refuse_smiles"]],
            )

            # Two stream modes on one run, because the two halves of the contract are published
            # on two channels: the model-facing message lands in graph state (`values`) and the
            # chemist's failure signal is written to the custom stream. Driving the turn twice
            # would let them disagree about the same call.
            state: Any = None
            signals: list[Signal] = []
            async for mode, payload in graph.astream(
                {"messages": [("user", "check that smiles")]}, stream_mode=["values", "custom"]
            ):
                if mode == "values":
                    state = payload
                elif isinstance(payload, dict) and isinstance(payload.get(_SIGNAL_KEY), Signal):
                    signals.append(payload[_SIGNAL_KEY])
            return state, signals

    state, signals = asyncio.run(_go())

    # The turn survived the failed step, and the model was answered without the retry flag.
    assert str(state["messages"][-1].content) == "done"
    (tool_message,) = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert tool_message.status == "success"
    assert "has an unclosed ring" in tool_message.text
    # ...while the durable trail still records the call as the failure it was.
    recorded = {event.tool: event for event in sink.events}
    assert recorded["refuse_smiles"].outcome == "error", (
        "clearing the model-facing flag also blanked the audit trail"
    )
    assert "has an unclosed ring" in recorded["refuse_smiles"].detail
    # ...and the chemist was still told the step did not work.
    failures = [signal for signal in signals if isinstance(signal, ToolFailureSignal)]
    assert [failure.tool for failure in failures] == ["refuse_smiles"], (
        f"the chemist was never told the step failed; saw {signals}"
    )
