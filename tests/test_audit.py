"""The tool-audit middleware records every call, once, without altering behavior.

A successful call is logged at INFO with name and arguments, a failing one at WARNING with the
exception propagated unchanged, and oversized arguments are truncated. The per-conversation
factory stamps correlation id and actor and hands each event to an injected sink, and a sink
failure never breaks the call.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from chemclaw.agent.audit import (
    AuditEvent,
    NullAuditSink,
    default_audit_sink,
    make_audit_middleware,
)
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.agent.subagents import HELPER_BRIEF
from chemclaw.core.config import settings
from tests.middleware import run_middleware, tool_request


def _ctx(name: str, arguments: object, result: object = None) -> Any:
    """The call as the audit middleware reads it: a name and its arguments.

    `result` is accepted and ignored: the middleware records what the handler returns, so tests pass
    it back from `call_next`.
    """
    return tool_request(name, dict(arguments) if isinstance(arguments, dict) else {})


def _drive(ctx: Any, call_next: Callable[[], Awaitable[Any]]) -> None:
    """Run the middleware with no explicit sink over a stand-in context.

    Log-only because the test config's `session_store="memory"` makes `default_audit_sink` resolve
    to the null sink.
    """
    mw = make_audit_middleware(correlation_id="-", actor=settings.service_actor_id)

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
    mw = make_audit_middleware(correlation_id="conv-9", actor="unknown", sink=sink)

    async def _ok_call() -> None:
        return None

    token = set_current_identity("u-entra-oid", frozenset({"compute"}))
    try:
        _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)
    finally:
        reset_current_identity(token)

    assert sink.events[0].actor == "u-entra-oid"  # ambient user, not the "unknown" fallback


def test_the_audit_row_leaves_agent_empty_for_the_agent_the_chemist_talks_to() -> None:
    """`agent` is empty on a caller's row, and every other audited field is untouched.

    The trail names an agent only when the call was made by a helper (driven in
    `test_the_trail_names_the_helper_that_made_a_call_and_leaves_the_caller_unnamed`); a chain built
    with no `agent=` records none. The row is otherwise field-for-field the stored shape.
    """
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-main", actor="alice@corp", sink=sink)

    async def _ok_call() -> None:
        return None

    _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)

    event = sink.events[0]
    assert event.model_dump() == {
        "correlation_id": "conv-main",
        "session_id": "",
        "purpose": "",
        "actor": "alice@corp",
        # Written in rather than excluded, as `tool_revision` is: a growing exclude set checks less
        # each time. Empty because no `agent=` was given, as for every caller but a helper.
        "agent": "",
        # Empty because this request carries no todo list — the plan step is read from
        # `request.state["todos"]`, and a request built outside the harness has none
        # (`D-2026-08-27-a-refusal-is-not-a-crash`).
        "plan_step": "",
        "tool": "find_notes",
        "arguments": "{'q': 'x'}",
        "outcome": "ok",
        "detail": "",
        "latency_ms": event.latency_ms,
        "revision": settings.deployment_revision,
        # Empty because `find_notes` ran in this process — see the two tests at the end of the file
        # for why that is a complete answer and not a gap.
        "tool_revision": "",
        # Stamped by the middleware when the call *started*, so a row is dated by the tool rather
        # than by whenever the batching sink drained. Read off the event for the same reason
        # `latency_ms` is: this asserts the field is present and the row's own, not its value.
        "ts": event.ts,
    }


class _TaskScript(GenericFakeChatModel):
    """A model that spawns one helper and has it call one tool — the two graphs of a real turn.

    The graphs are told apart by their prompt (only the helper's carries `HELPER_BRIEF`), which
    stays right however many model calls either side makes.
    """

    parent_calls: int = 0

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> Any:
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        # The first line rather than a prefix slice: a system message reaches a fake model as a
        # list of content blocks, so `str(...)` of it carries `\n` as two literal characters and a
        # slice that starts with the brief's own blank line matches nothing.
        blob = " ".join(str(getattr(m, "content", "")) for m in messages)
        if HELPER_BRIEF.strip().splitlines()[0] in blob:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "ls", "args": {}, "id": "h1", "type": "tool_call"}],
            )
            if any(getattr(m, "name", None) == "ls" for m in messages):
                message = AIMessage(content="nothing found")
        else:
            self.parent_calls += 1
            message = (
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "sweep the sources",
                                "subagent_type": "general-purpose",
                            },
                            "id": "t1",
                            "type": "tool_call",
                        }
                    ],
                )
                if self.parent_calls == 1
                else AIMessage(content="final answer")
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_the_trail_names_the_helper_that_made_a_call_and_leaves_the_caller_unnamed() -> None:
    """A helper's calls are marked as its own, the caller's are not, and both name the chemist.

    A helper is spawned on every turn on a brief the chemist never sees, so its calls must not land
    as the chemist's own. Driven through a real `task` spawn in the compiled graph, with rows read
    back off the sink, so a dropped argument anywhere between `build_langgraph_agent` and the sink
    fails. The agent is named beside the human, never instead
    (`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` invariant 3).
    """
    sink = _RecordingSink()
    graph = build_langgraph_agent(
        model=_TaskScript(messages=iter([])),
        profile=AgentProfile(name="default"),
        actor="alice@corp",
        audit_sink=sink,
    )
    asyncio.run(graph.ainvoke(turn_input("sweep the sources"), turn_config("agent-column")))

    by_tool = {event.tool: event.agent for event in sink.events}
    assert by_tool.get("ls") == "default-helper", (
        "a tool call made inside the helper is recorded with no agent, so the trail cannot tell it "
        f"from a call the chemist's own agent made: {by_tool}"
    )
    assert by_tool.get("task") == "", (
        "the caller's own calls must stay unnamed — the column says which agent ran a call when it "
        "was not the one the chemist is talking to"
    )
    assert {event.actor for event in sink.events} == {"alice@corp"}


def test_audit_stamps_the_deployment_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every recorded event carries the process's deployment revision (AG-14, provenance)."""
    monkeypatch.setattr(settings, "deployment_revision", "sha-abc123")
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-r", actor="a", sink=sink)

    async def _ok_call() -> None:
        return None

    _drive_mw(mw, _ctx("find_notes", {"q": "x"}), _ok_call)

    assert sink.events[0].revision == "sha-abc123"


def test_factory_stamps_correlation_id_actor_and_records_outcome() -> None:
    """The per-conversation middleware records cid, actor, outcome, and the result effect."""
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-1", actor="alice@corp", sink=sink)

    async def _returns_ref() -> str:
        return "pr://note/insight-1"

    # Returned by the tool rather than pre-set on the context, which is the shape the trail
    # actually records: `wrap_tool_call` middleware sees what the *handler* produced, where MAF's
    # wrote its result onto the invocation context for the middleware to read afterwards.
    ctx = _ctx("record_knowledge_note", {"type": "insight"})
    _drive_mw(mw, ctx, _returns_ref)

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.correlation_id == "conv-1"
    assert event.actor == "alice@corp"
    assert event.tool == "record_knowledge_note"
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
    """A broken audit sink is logged (alertably) and swallowed — the tool call still succeeds."""
    mw = make_audit_middleware(correlation_id="c", actor="a", sink=_BrokenSink())
    with caplog.at_level(logging.ERROR):
        _drive_mw(mw, _ctx("predict_pka", {"smiles": "CCO"}), _ok)  # must not raise
    # SEC-3: the lost audit record is logged at ERROR with a stable, greppable marker so it can
    # alert.
    record = next(r for r in caplog.records if "audit_sink_failure" in r.getMessage())
    assert record.levelno == logging.ERROR
    assert getattr(record, "event", None) == "audit_sink_failure"


def test_a_postgres_deployment_gets_the_durable_trail_without_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trail is durable wherever a database is configured — opting in is not required.

    Asserted at `default_audit_sink` rather than at a call site, so every entry point (front door,
    Temporal template activities, future ones) gets the Postgres sink.
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
    """`make_audit_middleware()` with no `sink` resolves the default, not `NullAuditSink`.

    The polarity that matters here: a forgotten argument must not downgrade the
    compliance record. Opting *out* stays possible by passing `NullAuditSink()` explicitly.
    """
    recorded: list[str] = []

    class _Marker:
        async def record(self, event: AuditEvent) -> None:
            recorded.append(event.tool)

    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: _Marker())
    middleware = make_audit_middleware(correlation_id="c", actor="a")
    context = _ctx("compute_xtb_energy", {"smiles": "CCO"})

    _drive_mw(middleware, context, _ok)

    assert recorded == ["compute_xtb_energy"], "the default sink was not consulted"


# --- a cancelled attempt is still an attempt -------------------------------------------------


class _SlowSink:
    """A sink whose write suspends before it records, and signals when it has.

    The suspension is where an unshielded write in the cancellation handler would be cancelled.
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


async def test_a_cancelled_tool_call_still_records_the_attempt() -> None:
    """A disconnect or turn deadline mid-tool leaves a `cancelled` row, not silence (D-130).

    `CancelledError` is a `BaseException`, so `except Exception` misses it; the attempt is recorded
    under its own outcome.
    """
    sink = _RecordingSink()
    middleware = make_audit_middleware(correlation_id="conv-cancel", actor="carol", sink=sink)

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

    assert [event.outcome for event in sink.events] == ["cancelled"]
    event = sink.events[0]
    assert (event.tool, event.actor, event.correlation_id) == (
        "compute_xtb_energy",
        "carol",
        "conv-cancel",
    )
    assert "CCO" in event.arguments  # the attempted inputs, which is half of what was attempted
    assert event.latency_ms > 0.0


async def test_the_cancelled_row_survives_a_second_cancellation() -> None:
    """The write is shielded, so the teardown that caused it cannot also erase it.

    Structured-concurrency teardowns re-deliver cancellation into cleanup awaits, so the write runs
    under `asyncio.shield`, as the runner's history rollback does.
    """
    sink = _SlowSink()
    middleware = make_audit_middleware(correlation_id="conv-torn", actor="dave", sink=sink)

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

    assert [event.outcome for event in sink.events] == ["cancelled"]


def test_the_row_names_the_server_build_beside_the_orchestrator_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row names the server build beside the orchestrator's.

    Connector tools run on another repository's release cadence, so `revision` alone cannot say
    whether a prompt or a solver changed. Both are asserted together because the likely mistake is
    one written into the other.
    """
    monkeypatch.setattr(settings, "deployment_revision", "orchestrator-abc123")
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-tr", actor="a", sink=sink)
    served = _served_tool("predict_pka", connector="calc", revision="server-9f3c1d")

    _drive_mw(mw, tool_request("predict_pka", {"smiles": "CCO"}, tool=served), _ok)

    event = sink.events[0]
    assert event.revision == "orchestrator-abc123"
    assert event.tool_revision == "calc@server-9f3c1d"


def test_an_in_process_tool_records_no_server_build_rather_than_a_fabricated_one() -> None:
    """Empty is the complete answer here, and `"unknown"` would be a false alarm.

    An in-process tool's build is `revision`; `<connector>@unknown` is reserved for an image shipped
    without its revision, which someone should fix.
    """
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-ip", actor="a", sink=sink)

    _drive_mw(mw, tool_request("write_todos", {}), _ok)

    assert sink.events[0].tool_revision == ""


def _served_tool(name: str, *, connector: str, revision: str) -> Any:
    """A tool stamped the way `connectors/transport.py::_stamped` stamps a connector's tools.

    Built through `_stamped`, so the test fails if the transport stops writing the key.
    """
    from langchain_core.tools import tool as make_tool

    from chemclaw.connectors.transport import _stamped

    @make_tool
    def _probe() -> str:
        """A trivial stand-in for a connector tool."""
        return "ok"

    _probe.name = name
    return _stamped([_probe], connector=connector, revision=revision)[0]


@pytest.mark.parametrize("message_class", ["ToolMessage", "ToolMessageChunk"])
def test_a_connector_failure_is_recorded_as_an_error_however_it_is_streamed(
    message_class: str,
) -> None:
    """An MCP tool never raises, and on a streaming run its result is not a `ToolMessage`.

    `returned_failure` keeps a returned `isError=True` result from being audited as success. It uses
    `isinstance` because `ToolMessageChunk` is a subclass; parametrised over both classes.
    """
    from langchain_core.messages import ToolMessage, ToolMessageChunk

    built = {"ToolMessage": ToolMessage, "ToolMessageChunk": ToolMessageChunk}[message_class]
    sink = _RecordingSink()
    mw = make_audit_middleware(correlation_id="conv-3", actor="alice@corp", sink=sink)

    async def _returns_failure() -> Any:
        return built(content="Error: the instrument is offline", tool_call_id="c-1", status="error")

    _drive_mw(mw, _ctx("screen_hazards", {}), _returns_failure)

    assert [event.outcome for event in sink.events] == ["error"], (
        f"a connector failure arriving as a {message_class} was audited as a successful call"
    )
    assert "instrument is offline" in sink.events[0].detail


def test_a_log_only_trail_is_announced_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The front door says out loud that it is keeping no durable record.

    `default_audit_sink()` is the null sink whenever `session_store != "postgres"`, even with a
    database present, so startup warns and names the setting that fixes it.
    """
    from chemclaw.api.app import _report_inventory

    monkeypatch.setattr(settings, "session_store", "memory")
    with caplog.at_level(logging.WARNING):
        _report_inventory()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a log-only trail was not announced at all"
    assert "NullAuditSink" in warnings[0].message
    assert "CHEMCLAW_SESSION_STORE=postgres" in warnings[0].message


def test_a_durable_trail_is_not_warned_about(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other side of the same line: a deployment that writes the trail hears nothing.

    Without this the warning is one that always fires, which is a warning nobody reads.
    """
    from chemclaw.api.app import _report_inventory

    monkeypatch.setattr(settings, "session_store", "postgres")
    with caplog.at_level(logging.WARNING):
        _report_inventory()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_startup_inventory_names_every_subsystem_that_can_be_silently_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup inventory names every subsystem that can be silently empty.

    Connectors, the audit trail, the session store, skills, ingest sources and result sinks, each a
    term an operator can compare against what they configured.
    """
    from chemclaw.api.app import startup_inventory

    monkeypatch.setattr(settings, "result_sinks", "")
    terms = dict(term.split("=", 1) for term in startup_inventory())
    assert set(terms) == {
        "audit-trail",
        "sessions",
        "skills",
        "knowledge-notes",
        "data-sources",
        "result-sinks",
        "vector-store",
    }
    assert terms["result-sinks"].startswith("none")
