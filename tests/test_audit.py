"""The tool-audit middleware records every call, once, without altering behavior.

Proves the GxP audit trail: a successful tool call is logged at INFO with its name and
arguments, a failing one is logged at WARNING and the exception propagates unchanged, and
oversized arguments are truncated to the configured budget. It also proves the durable seam:
the per-conversation factory stamps a correlation id and actor and hands each event to an
injected sink, and a sink failure never breaks the tool call. A light stand-in context is
enough — no live agent run or model call is needed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from agent_framework import FunctionInvocationContext

from agents.audit import (
    AuditEvent,
    audit_tool_calls,
    make_audit_middleware,
)
from chemclaw.config import settings


def _ctx(name: str, arguments: object, result: object = None) -> FunctionInvocationContext:
    """A minimal stand-in exposing only the fields the middleware reads."""
    return cast(
        FunctionInvocationContext,
        SimpleNamespace(function=SimpleNamespace(name=name), arguments=arguments, result=result),
    )


def _drive(ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
    """Run the middleware over a stand-in context to completion."""

    async def _run() -> None:
        await audit_tool_calls(ctx, call_next)

    asyncio.run(_run())


async def _ok() -> None:
    """A tool body that succeeds."""
    return None


async def _boom() -> None:
    """A tool body that raises."""
    raise ValueError("boom")


def test_audit_logs_a_successful_call(caplog: pytest.LogCaptureFixture) -> None:
    """A successful invocation logs one INFO line naming the tool and its arguments."""
    with caplog.at_level(logging.INFO):
        _drive(_ctx("predict_solubility", {"smiles": "CCO"}), _ok)
    assert "tool predict_solubility ok" in caplog.text
    assert "CCO" in caplog.text  # the argument is captured for the audit trail


def test_audit_logs_and_reraises_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """A failing tool logs at WARNING and the original exception propagates unchanged."""
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="boom"):
            _drive(_ctx("compute_xtb_energy", {}), _boom)
    assert "tool compute_xtb_energy failed" in caplog.text
    assert "boom" in caplog.text


def test_audit_truncates_oversized_arguments(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large argument payload is truncated to the configured budget, not logged whole."""
    monkeypatch.setattr(settings, "agent_audit_max_arg_chars", 10)
    with caplog.at_level(logging.INFO):
        _drive(_ctx("gather_evidence", {"q": "x" * 500}), _ok)
    assert "…" in caplog.text  # truncation marker present
    assert "x" * 100 not in caplog.text  # the full payload never reaches the log


class _RecordingSink:
    """An `AuditSink` that keeps every event, to assert what the middleware emits."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class _BrokenSink:
    """An `AuditSink` that always fails, to prove a sink error never breaks the tool call."""

    async def record(self, event: AuditEvent) -> None:
        raise RuntimeError("audit store down")


def _drive_mw(
    mw: object, ctx: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
) -> None:
    """Run an arbitrary middleware over a stand-in context to completion."""

    async def _run() -> None:
        await mw(ctx, call_next)  # type: ignore[operator]

    asyncio.run(_run())


def test_ambient_identity_overrides_the_static_actor() -> None:
    """The turn's authenticated Entra user is the recorded actor, over the build default (F4)."""
    from agents.identity_context import reset_current_identity, set_current_identity

    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-9", actor="unknown", sink=sink)

    async def _ok_call() -> None:
        return None

    token = set_current_identity("u-entra-oid", frozenset({"compute"}))
    try:
        _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)
    finally:
        reset_current_identity(token)

    assert sink.events[0].actor == "u-entra-oid"  # ambient user, not the "unknown" fallback


def test_factory_stamps_correlation_id_actor_and_records_outcome() -> None:
    """The per-conversation middleware records cid, actor, outcome, and the result effect."""
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-1", actor="alice@corp", sink=sink)

    async def _returns_ref() -> None:
        return None

    ctx = _ctx("propose_knowledge_note", {"type": "insight"}, result="pr://note/insight-1")
    _drive_mw(mw, ctx, _returns_ref)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.correlation_id == "conv-1"
    assert event.actor == "alice@corp"
    assert event.tool == "propose_knowledge_note"
    assert event.outcome == "ok"
    assert "pr://note/insight-1" in event.detail  # the effect is captured


def test_factory_records_failure_and_reraises() -> None:
    """A failing tool records an `error` event and still propagates the exception."""
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-2", actor="bob", sink=sink)
    with pytest.raises(ValueError, match="boom"):
        _drive_mw(mw, _ctx("compute_xtb_energy", {}), _boom)
    assert sink.events[0].outcome == "error"
    assert "boom" in sink.events[0].detail


def test_sink_failure_does_not_break_the_tool_call(caplog: pytest.LogCaptureFixture) -> None:
    """A broken audit sink is logged and swallowed — the tool call still succeeds."""
    mw = make_audit_middleware(correlation_id="c", actor="a", sink=_BrokenSink())
    with caplog.at_level(logging.WARNING):
        _drive_mw(mw, _ctx("predict_pka", {"smiles": "CCO"}), _ok)  # must not raise
    assert "audit sink failed" in caplog.text
