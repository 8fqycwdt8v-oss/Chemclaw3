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
from typing import Any

import pytest

from chemclaw.agent.audit import (
    AuditEvent,
    NullAuditSink,
    default_audit_sink,
    make_langgraph_audit_middleware,
)
from chemclaw.core.config import settings
from tests.middleware import run_middleware, tool_request


def _ctx(name: str, arguments: object, result: object = None) -> Any:
    """The call as the audit middleware reads it: a name and its arguments.

    `result` is accepted and ignored. A `wrap_tool_call` middleware records what the *handler*
    returns rather than what the caller pre-set on a context, so the tests that care pass it back
    from their `call_next` instead — which is the more honest arrangement, since the trail is
    supposed to record what the tool produced.
    """
    return tool_request(name, dict(arguments) if isinstance(arguments, dict) else {})


def _drive(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run the middleware with no explicit sink over a stand-in context.

    Log-only in practice because the test config leaves `session_store="memory"`, which is what
    `default_audit_sink` resolves to — not because omitting `sink` means log-only (it no longer
    does; see `test_an_omitted_sink_no_longer_silently_means_log_only`).
    """
    mw = make_langgraph_audit_middleware(correlation_id="-", actor=settings.service_actor_id)

    async def _handler(_request: Any) -> Any:
        return await call_next()

    asyncio.run(run_middleware(mw, ctx, _handler))


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


def _as_handler(call_next: Callable[[], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
    """Adapt a zero-arg tool body to the `handler(request)` a middleware calls."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    return _handler


def _drive_mw(mw: Any, ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run an arbitrary middleware over one call to completion."""

    async def _handler(_request: Any) -> Any:
        return await call_next()

    asyncio.run(run_middleware(mw, ctx, _handler))


def test_ambient_identity_overrides_the_static_actor() -> None:
    """The turn's authenticated Entra user is the recorded actor, over the build default (F4)."""
    from chemclaw.core.identity_context import reset_current_identity, set_current_identity

    sink = _RecordingSink()
    mw = make_langgraph_audit_middleware(correlation_id="conv-9", actor="unknown", sink=sink)

    async def _ok_call() -> None:
        return None

    token = set_current_identity("u-entra-oid", frozenset({"compute"}))
    try:
        _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)
    finally:
        reset_current_identity(token)

    assert sink.events[0].actor == "u-entra-oid"  # ambient user, not the "unknown" fallback


def test_a_specialist_is_recorded_beside_the_human_actor() -> None:
    """The trail names both: which person authorized the turn, and which agent made the call.

    Invariant 3 of D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor. A subagent is an
    attenuation of its caller's authority, not a new actor, so recording only the specialist would
    make the trail worthless under GxP and recording it *as* the actor would repeat the D-040
    failure — an agent's act attributed to a chemist's Entra oid. Both fields, or neither is true.
    """
    from chemclaw.core.identity_context import (
        reset_current_identity,
        reset_current_specialist,
        set_current_identity,
        set_current_specialist,
    )

    sink = _RecordingSink()
    mw = make_langgraph_audit_middleware(correlation_id="conv-sub", actor="unknown", sink=sink)

    async def _ok_call() -> None:
        return None

    identity = set_current_identity("u-entra-oid", frozenset({"compute"}))
    specialist = set_current_specialist("computation")
    try:
        _drive_mw(mw, _ctx("predict_pka", {"smiles": "CCO"}), _ok_call)
    finally:
        reset_current_specialist(specialist)
        reset_current_identity(identity)

    event = sink.events[0]
    assert event.actor == "u-entra-oid", "the human authorization was lost"
    assert event.agent == "computation", "the specialist that ran the call was lost"


def test_the_main_agent_records_an_empty_specialist_and_nothing_else_changes() -> None:
    """Outside a subgraph the field is empty — and every other audited field is untouched.

    Empty is the honest record for the turn's own agent, not a gap. The second half is the one that
    matters for the trail already in the database: widening the event must not perturb what a call
    with no specialist records, so the row a main-agent call produces is field-for-field what it was
    before `agent` existed. `test_the_versioned_hash_reproduces_the_v2_bytes_exactly`
    (`tests/test_audit_chain.py`) is the same claim at the level of the bytes that get hashed.
    """
    from chemclaw.core.identity_context import (
        get_current_specialist,
        reset_current_specialist,
        set_current_specialist,
    )

    sink = _RecordingSink()
    mw = make_langgraph_audit_middleware(correlation_id="conv-main", actor="alice@corp", sink=sink)

    async def _ok_call() -> None:
        return None

    _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)

    event = sink.events[0]
    assert event.agent == ""
    assert event.model_dump(exclude={"agent"}) == {
        "correlation_id": "conv-main",
        "session_id": "",
        "purpose": "",
        "actor": "alice@corp",
        "tool": "find_notes",
        "arguments": "{'q': 'x'}",
        "outcome": "ok",
        "detail": "",
        "latency_ms": event.latency_ms,
        "revision": settings.deployment_revision,
    }
    # And the binding is scoped to the subgraph, not leaked into the turn that followed it.
    token = set_current_specialist("safety")
    reset_current_specialist(token)
    assert get_current_specialist() == ""


def test_a_nested_specialist_restores_its_caller_rather_than_clearing_it() -> None:
    """A specialist that delegates further leaves its own name behind, not an empty string.

    `reset_current_specialist` restores the previous value precisely so a two-level delegation
    attributes the outer specialist's own later calls to it — clearing instead would silently
    re-attribute them to the main agent, which is a false record rather than a missing one.
    """
    from chemclaw.core.identity_context import (
        get_current_specialist,
        reset_current_specialist,
        set_current_specialist,
    )

    outer = set_current_specialist("design")
    inner = set_current_specialist("safety")
    assert get_current_specialist() == "safety"
    reset_current_specialist(inner)
    assert get_current_specialist() == "design"
    reset_current_specialist(outer)
    assert get_current_specialist() == ""


def test_audit_stamps_the_deployment_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every recorded event carries the process's deployment revision (AG-14, GxP provenance)."""
    monkeypatch.setattr(settings, "deployment_revision", "sha-abc123")
    sink = _RecordingSink()
    mw = make_langgraph_audit_middleware(correlation_id="conv-r", actor="a", sink=sink)

    async def _ok_call() -> None:
        return None

    _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)

    assert sink.events[0].revision == "sha-abc123"


def test_factory_stamps_correlation_id_actor_and_records_outcome() -> None:
    """The per-conversation middleware records cid, actor, outcome, and the result effect."""
    sink = _RecordingSink()
    mw = make_langgraph_audit_middleware(correlation_id="conv-1", actor="alice@corp", sink=sink)

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
    mw = make_langgraph_audit_middleware(correlation_id="conv-2", actor="bob", sink=sink)
    with pytest.raises(ValueError, match="boom"):
        _drive_mw(mw, _ctx("compute_xtb_energy", {}), _boom)
    assert sink.events[0].outcome == "error"
    assert "boom" in sink.events[0].detail


def test_sink_failure_does_not_break_the_tool_call(caplog: pytest.LogCaptureFixture) -> None:
    """A broken audit sink is logged (alertably) and swallowed — the tool call still succeeds."""
    mw = make_langgraph_audit_middleware(correlation_id="c", actor="a", sink=_BrokenSink())
    with caplog.at_level(logging.ERROR):
        _drive_mw(mw, _ctx("predict_pka", {"smiles": "CCO"}), _ok)  # must not raise
    # SEC-3: the lost GxP record is logged at ERROR with a stable, greppable marker so it can alert.
    record = next(r for r in caplog.records if "audit_sink_failure" in r.getMessage())
    assert record.levelno == logging.ERROR
    assert getattr(record, "event", None) == "audit_sink_failure"


def test_a_postgres_deployment_gets_the_durable_trail_without_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GxP trail is durable wherever a database is configured — opting in is not required.

    The regression test for the pass's highest-ranked finding. `PostgresAuditSink`, the
    tamper-evident chain, `infra/sql/011` and `make audit-verify` were all built and tested, and
    the sink was constructed in exactly one place — `cli/chat.py`, behind a flag. The deployed
    service passed no sink, so the middleware installed `NullAuditSink()` and `audit_events` was
    empty in production while every document called it the compliance record.

    Asserted at `default_audit_sink` rather than at a call site on purpose: fixing the service's
    factory alone would have left the identical trap set for the Temporal template activities
    (which had it independently) and for every entry point added later.
    """
    from chemclaw.agent.audit_store import PostgresAuditSink

    monkeypatch.setattr(settings, "session_store", "postgres")
    assert isinstance(default_audit_sink(), PostgresAuditSink)


def test_a_deployment_with_no_database_falls_back_to_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Postgres the sink is log-only, not a sink that raises on every tool call."""
    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_audit_sink(), NullAuditSink)


def test_an_omitted_sink_no_longer_silently_means_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make_langgraph_audit_middleware()` with no `sink` resolves the default, not `NullAuditSink`.

    The polarity that matters for a GxP control: a forgotten argument must not downgrade the
    compliance record. Opting *out* stays possible by passing `NullAuditSink()` explicitly.
    """
    recorded: list[str] = []

    class _Marker:
        async def record(self, event: AuditEvent) -> None:
            recorded.append(event.tool)

    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: _Marker())
    middleware = make_langgraph_audit_middleware(correlation_id="c", actor="a")
    context = _ctx("compute_xtb_energy", {"smiles": "CCO"})

    _drive_mw(middleware, context, _ok)

    assert recorded == ["compute_xtb_energy"], "the default sink was not consulted"


# --- a cancelled attempt is still an attempt -------------------------------------------------


class _SlowSink:
    """A sink whose write suspends before it records, and signals when it has.

    The suspension is the point: it is the moment a plain `await _emit(...)` inside the
    cancellation handler would be cancelled and write nothing, so a sink that records
    synchronously could not tell the shielded writer from the broken one.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.written = asyncio.Event()

    async def record(self, event: AuditEvent) -> None:
        await asyncio.sleep(0)
        self.events.append(event)
        self.written.set()


def _hangs_until(started: asyncio.Event) -> Callable[[], Awaitable[Any]]:
    """A tool body that announces it is running and then never returns."""

    async def _call() -> None:
        started.set()
        await asyncio.sleep(3600)

    return _call


def test_a_cancelled_tool_call_still_records_the_attempt() -> None:
    """A disconnect or turn deadline mid-tool leaves a `cancelled` row, not silence (D-130).

    `CancelledError` is a `BaseException`, so the `except Exception` that records a failure never
    saw it: every tool call interrupted by a client disconnect or the front door's turn deadline
    left no row at all, and the GxP trail under-reported *attempted* calls exactly when a turn went
    wrong. The attempt is what the trail is for, so it is recorded under its own outcome — a
    cancellation is neither a success nor a tool failure.
    """
    sink = _RecordingSink()
    middleware = make_langgraph_audit_middleware(
        correlation_id="conv-cancel", actor="carol", sink=sink
    )

    async def _run() -> None:
        started = asyncio.Event()
        task = asyncio.ensure_future(
            run_middleware(
                middleware,
                _ctx("compute_xtb_energy", {"smiles": "CCO"}),
                _as_handler(_hangs_until(started)),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert [event.outcome for event in sink.events] == ["cancelled"]
    event = sink.events[0]
    assert (event.tool, event.actor, event.correlation_id) == (
        "compute_xtb_energy",
        "carol",
        "conv-cancel",
    )
    assert "CCO" in event.arguments  # the attempted inputs, which is half of what was attempted
    assert event.latency_ms > 0.0


def test_the_cancelled_row_survives_a_second_cancellation() -> None:
    """The write is shielded, so the teardown that caused it cannot also erase it.

    A structured-concurrency teardown does not cancel once: sse-starlette's task group and
    `asyncio.timeout` both re-deliver the cancellation into any `await` the cleanup makes. A plain
    `await` on the audit write would therefore be cancelled at the sink's first suspension point
    and record nothing — the same missing row, moved one frame later. `asyncio.shield` puts the
    write on its own task, the pattern `chemclaw.api.runner` already uses for the history rollback.
    """
    sink = _SlowSink()
    middleware = make_langgraph_audit_middleware(
        correlation_id="conv-torn", actor="dave", sink=sink
    )

    async def _run() -> None:
        started = asyncio.Event()
        task = asyncio.ensure_future(
            run_middleware(
                middleware,
                _ctx("gather_evidence", {"query": "biaryl"}),
                _as_handler(_hangs_until(started)),
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)  # let the middleware reach its cancellation handler
        task.cancel()  # the re-delivery a task group makes while the handler is awaiting
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(sink.written.wait(), timeout=5.0)

    asyncio.run(_run())

    assert [event.outcome for event in sink.events] == ["cancelled"]
