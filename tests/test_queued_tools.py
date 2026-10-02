"""Queued tool calls: a heavy call waits for a slot in one global queue instead of being refused.

What is asserted, in the order a call travels:

- the manifest refuses a queued tool the endpoint does not serve;
- a turn's connector session routes exactly the queued tools through the queue and leaves every
  other tool on the direct path, with the tool object the agent binds unchanged;
- the activity, against a real MCP server, returns an answer or a domain refusal as the server's
  own `CallToolResult`, and turns only a *full* server into the retryable failure;
- end to end on Temporal: an inline answer, a full server retried until it admits, identical
  concurrent calls sharing one run, and a call that outlasts the inline wait handing back a job id
  and delivering its answer to the session afterwards.

The Temporal-backed half skips where the test server cannot be fetched, like every workflow test
here; everything above it runs everywhere.
"""

import asyncio
import dataclasses
import threading
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from chemclaw.connectors.calc.remote import CalcBusyError
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint, QueuedDispatch
from chemclaw.connectors.queued import dispatch_queued, queued_workflow_id
from chemclaw.connectors.queued_call import AT_CAPACITY_TYPE, QueuedToolCall, call_queued_tool
from chemclaw.connectors.queued_workflow import RESULT_KEY, QueuedToolWorkflow, envelope
from chemclaw.connectors.queues import interactive_queue
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.connectors.server import connector_app
from chemclaw.connectors.transport import ConnectorSpec
from chemclaw.durable.notify import SessionEventInput
from tests.conftest import _free_port
from tests.temporal_env import pydantic_client, start_local_env_or_skip
from tests.test_connector_transport import _endpoint, _Server

_CONNECTOR = "queued-probe"


def _queued_endpoint(url: str, *tools: str, queued: tuple[str, ...]) -> HttpEndpoint:
    """A loopback endpoint serving `tools`, of which `queued` go through the queue."""
    return HttpEndpoint(
        url=url,
        tools=list(tools),
        state_changing=list(tools),
        queued=QueuedDispatch(tools=list(queued), inline_wait_seconds=5),
    )


def test_a_queued_tool_the_endpoint_does_not_serve_is_refused() -> None:
    """A name that matches nothing would queue nothing while reading as if it did."""
    with pytest.raises(ValidationError, match="does not serve"):
        _queued_endpoint("http://127.0.0.1:1/mcp", "a", queued=("b",))


def test_a_queued_read_is_accepted_because_cost_is_not_side_effect() -> None:
    """`rxnpredict`'s predictions are reads that cost seconds of CPU: classification is not cost."""
    endpoint = HttpEndpoint(
        url="http://127.0.0.1:1/mcp",
        tools=["predict"],
        read_only=["predict"],
        queued=QueuedDispatch(tools=["predict"], inline_wait_seconds=5),
    )
    assert endpoint.queued is not None


def test_identical_calls_share_an_id_and_different_ones_do_not() -> None:
    """The id is the call, never the caller — which is what lets concurrent asks join one run."""
    first = queued_workflow_id("calc", "predict_pka", {"smiles": "CCO"})
    assert first == queued_workflow_id("calc", "predict_pka", {"smiles": "CCO"})
    assert first != queued_workflow_id("calc", "predict_pka", {"smiles": "CCN"})
    assert first != queued_workflow_id("calc", "compute_xtb_energy", {"smiles": "CCO"})


def _probe_server() -> FastMCP:
    """A server with a queued tool, a direct one, a full one and a refusing one."""
    server = FastMCP(_CONNECTOR)

    @server.tool()
    async def heavy(smiles: str) -> str:
        """Answer directly — reached only if the queue is bypassed."""
        return f"direct:{smiles}"

    @server.tool()
    async def light() -> str:
        """A tool that is not queued."""
        return "light answered directly"

    @server.tool()
    async def full() -> str:
        """Refuse as a full backend behind one of this repository's own bundles does."""
        raise CalcBusyError("the calculation service is busy")

    @server.tool()
    async def refuse() -> str:
        """Refuse the input, as a domain error does."""
        raise ValueError("unknown solvent 'mud'")

    return server


def test_a_turn_routes_only_the_queued_tools_through_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queued tool reaches `dispatch_queued`; the other goes straight to the server.

    Through `open_connector_specs`, the path every turn takes, against a real server — so the
    property is the adapter's interceptor being installed where a turn builds its tools, not a
    function called in isolation.
    """
    dispatched: list[tuple[str, str, dict[str, Any]]] = []

    async def _queue(connector: str, tool: str, arguments: dict[str, Any], **_: Any) -> Any:
        dispatched.append((connector, tool, arguments))
        return CallToolResult(content=[TextContent(type="text", text="queued:answer")])

    monkeypatch.setattr("chemclaw.connectors.queued.dispatch_queued", _queue)
    app = connector_app(_probe_server(), name=_CONNECTOR)
    port = _free_port()
    endpoint = _queued_endpoint(f"http://127.0.0.1:{port}/mcp", "heavy", "light", queued=("heavy",))

    async def _call() -> tuple[str, str]:
        spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=_CONNECTOR)), endpoint)
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            assert not unreachable
            by_name = {tool.name: tool for tool in tools}
            heavy = await by_name["heavy"].ainvoke({"smiles": "CCO"})
            light = await by_name["light"].ainvoke({})
            return str(heavy), str(light)
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(app, port):
        heavy, light = asyncio.run(_call())
    assert "queued:answer" in heavy
    assert "light answered directly" in light
    assert dispatched == [(_CONNECTOR, "heavy", {"smiles": "CCO"})]


def test_an_unreachable_queue_falls_back_to_the_direct_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker outage must not take down a capability whose server is up.

    Nothing was started, so the call goes where it went before queues existed — and the answer is
    the server's own, which is what the chemist gets.
    """
    from chemclaw.core.errors import SubsystemUnavailableError

    async def _down() -> Any:
        raise SubsystemUnavailableError("the durable execution backend (Temporal) is unreachable")

    monkeypatch.setattr("chemclaw.connectors.queued.connect", _down)
    monkeypatch.setattr("chemclaw.connectors.queued.require_actor", lambda: "oid-chemist")
    app = connector_app(_probe_server(), name=_CONNECTOR)
    port = _free_port()
    endpoint = _queued_endpoint(f"http://127.0.0.1:{port}/mcp", "heavy", queued=("heavy",))

    async def _call() -> str:
        spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=_CONNECTOR)), endpoint)
        async with AsyncExitStack() as stack:
            tools, _ = await open_connector_specs(stack, [spec])
            return str(await tools[0].ainvoke({"smiles": "CCO"}))
        raise AssertionError("unreachable")  # pragma: no cover

    with _Server(app, port):
        assert "direct:CCO" in asyncio.run(_call())


def test_both_queue_counters_are_declared() -> None:
    """`record_metric` swallows an undeclared counter's `KeyError`: a typo would count nothing."""
    from chemclaw.core.metrics import METRICS

    for name in ("chemclaw_queued_tool_calls_total", "chemclaw_queued_tool_calls_direct_total"):
        METRICS.increment(name, labels={"tool": "predict_pka"})


def _spec_for(port: int) -> ConnectorSpec:
    """The spec the worker would build for the probe connector."""
    return _mcp_connection(
        cast(ConnectorManifest, SimpleNamespace(name=_CONNECTOR)),
        _endpoint(f"http://127.0.0.1:{port}/mcp", "heavy", "light", "full", "refuse"),
    )


def _call(tool: str, **arguments: Any) -> QueuedToolCall:
    return QueuedToolCall(
        connector=_CONNECTOR,
        tool=tool,
        arguments=arguments,
        call_timeout_seconds=10,
        queue_timeout_seconds=60,
    )


def test_the_activity_returns_answers_and_refusals_and_retries_only_a_full_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against a real server: an answer and a domain refusal come back as the server sent them.

    A full server is the one outcome raised, as the retryable `ConnectorAtCapacity` — a refusal
    is the tool's answer and asking again cannot change it, while a full pod admits the identical
    call a moment later.
    """
    port = _free_port()
    monkeypatch.setattr("chemclaw.connectors.registry.connector_spec", lambda name: _spec_for(port))
    app = connector_app(_probe_server(), name=_CONNECTOR)
    env = ActivityEnvironment()
    with _Server(app, port):
        answered = asyncio.run(env.run(call_queued_tool, _call("heavy", smiles="CCO")))
        refused = asyncio.run(env.run(call_queued_tool, _call("refuse")))
        with pytest.raises(ApplicationError) as full:
            asyncio.run(env.run(call_queued_tool, _call("full")))
    assert CallToolResult.model_validate(answered).content[0].text == "direct:CCO"  # type: ignore[union-attr]
    assert refused["isError"] is True
    assert "unknown solvent 'mud'" in str(refused["content"])
    assert full.value.type == AT_CAPACITY_TYPE
    assert not full.value.non_retryable


def test_a_queued_call_touches_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive worker holds no Postgres pool, and the connection budget counts on it.

    `chemclaw.fleetPools` charges it nothing (measured: an idle worker opened zero connections), so
    the activity must never borrow one. Every way into the database goes through `db.connection`
    or `db.pooled_connection`; both are made to fail here and a real call is made.
    """
    from chemclaw.core import db

    def _refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a queued call opened a database connection")

    for name in ("connect", "connection", "_pool_for"):
        monkeypatch.setattr(db, name, _refuse)
    port = _free_port()
    monkeypatch.setattr("chemclaw.connectors.registry.connector_spec", lambda name: _spec_for(port))
    with _Server(connector_app(_probe_server(), name=_CONNECTOR), port):
        answered = asyncio.run(
            ActivityEnvironment().run(call_queued_tool, _call("heavy", smiles="CCO"))
        )
    assert answered["content"][0]["text"] == "direct:CCO"


def test_an_unreachable_connector_is_retried_a_few_times_and_then_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault that is not a full server gets `queued_tool_fault_attempts` tries, not an hour."""
    port = _free_port()  # nothing listens here
    monkeypatch.setattr("chemclaw.connectors.registry.connector_spec", lambda name: _spec_for(port))
    monkeypatch.setattr("chemclaw.core.config.settings.queued_tool_fault_attempts", 2)
    first = ActivityEnvironment()
    first.info = dataclasses.replace(first.info, attempt=1)
    last = ActivityEnvironment()
    last.info = dataclasses.replace(last.info, attempt=2)
    with pytest.raises(ApplicationError) as early:
        asyncio.run(first.run(call_queued_tool, _call("heavy", smiles="C")))
    with pytest.raises(ApplicationError) as final:
        asyncio.run(last.run(call_queued_tool, _call("heavy", smiles="C")))
    assert not early.value.non_retryable
    assert final.value.non_retryable


def test_the_envelope_carries_the_server_s_answer_and_its_own_words() -> None:
    """Every job collector decodes the envelope, so a detached call needs no branch of its own."""
    raw = CallToolResult(content=[TextContent(type="text", text="pKa 15.9")]).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    result = envelope(_call("predict_pka"), raw)
    assert result.summary == "predict_pka answered: pKa 15.9"
    assert result.data[RESULT_KEY] == raw


# --------------------------------------------------------------------------- Temporal, end to end


class _Backend:
    """A stand-in for the server behind the activity: full for a while, then slow, then answering.

    Registered under the real activity's name, so the workflow's wiring — queue, timeouts, retry
    policy, the memo it passes — is the production wiring and only the network is replaced.
    """

    def __init__(
        self, *, full_for: int = 0, gate: threading.Event | None = None, hold: float = 0.0
    ) -> None:
        self.full_for = full_for
        self.gate = gate
        self.hold = hold
        self.calls = 0

    def activity(self) -> Any:
        @activity.defn(name="call_queued_tool")
        async def fake(
            call: QueuedToolCall, actor: str = "", correlation_id: str = "", session_id: str = ""
        ) -> dict[str, Any]:
            self.calls += 1
            if self.calls <= self.full_for:
                raise ApplicationError("full", type=AT_CAPACITY_TYPE)
            if self.gate is not None:
                while not self.gate.is_set():
                    await asyncio.sleep(0.05)
            await asyncio.sleep(self.hold)
            text = f"{call.tool}:{call.arguments} for {actor}"
            return CallToolResult(content=[TextContent(type="text", text=text)]).model_dump(
                mode="json", by_alias=True, exclude_none=True
            )

        return fake


def _bind_turn(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """The ambient a turn provides: a broker client, an actor, a session, a signal sink."""

    async def _connect() -> Any:
        return client

    monkeypatch.setattr("chemclaw.connectors.queued.connect", _connect)
    monkeypatch.setattr("chemclaw.connectors.queued.require_actor", lambda: "oid-chemist")
    monkeypatch.setattr("chemclaw.connectors.queued.get_current_session_id", lambda: "session-a")
    monkeypatch.setattr("chemclaw.core.config.settings.queued_tool_retry_max_seconds", 0.2)


async def _run_with_workers(
    backend: _Backend, body: Any, notified: list[Any], *, concurrency: int | None = None
) -> Any:
    """Run `body(client)` with the interactive worker and a push-back sink running."""
    env = await start_local_env_or_skip()
    async with env:
        client = pydantic_client(env)

        @activity.defn(name="record_session_event_activity")
        async def _record(event: SessionEventInput) -> None:
            notified.append((event.session_id, event.kind, event.payload))

        from chemclaw.core.config import settings

        async with (
            Worker(
                client,
                task_queue=interactive_queue(_CONNECTOR),
                workflows=[QueuedToolWorkflow],
                activities=[backend.activity()],
                max_concurrent_activities=concurrency,
            ),
            Worker(client, task_queue=settings.background_task_queue, activities=[_record]),
        ):
            return await body(client)


def test_a_queued_call_answers_inside_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case: a slot is free, the answer comes back as the tool's own result."""
    backend = _Backend()

    async def body(client: Any) -> CallToolResult:
        _bind_turn(monkeypatch, client)
        return await dispatch_queued(
            _CONNECTOR, "heavy", {"smiles": "CCO"}, inline_wait=20, call_timeout=10
        )

    result = asyncio.run(_run_with_workers(backend, body, []))
    assert not result.isError
    # Exactly the server's answer: a call that never waited carries no word about a queue.
    assert [cast(TextContent, block).text for block in result.content] == [
        "heavy:{'smiles': 'CCO'} for oid-chemist"
    ]


def test_a_full_server_is_asked_again_until_it_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three refusals and then an answer, all inside the turn — no refusal reaches the chemist."""
    backend = _Backend(full_for=3)

    async def body(client: Any) -> CallToolResult:
        _bind_turn(monkeypatch, client)
        return await dispatch_queued(
            _CONNECTOR, "heavy", {"smiles": "C"}, inline_wait=30, call_timeout=10
        )

    result = asyncio.run(_run_with_workers(backend, body, []))
    assert not result.isError
    assert backend.calls == 4


def test_identical_concurrent_calls_share_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Five chemists asking for the same thing at once pay for it once."""
    gate = threading.Event()
    backend = _Backend(gate=gate)

    async def body(client: Any) -> list[CallToolResult]:
        _bind_turn(monkeypatch, client)
        asks = [
            asyncio.create_task(
                dispatch_queued(
                    _CONNECTOR, "heavy", {"smiles": "CCO"}, inline_wait=30, call_timeout=10
                )
            )
            for _ in range(5)
        ]
        await asyncio.sleep(1.0)
        gate.set()
        return await asyncio.gather(*asks)

    results = asyncio.run(_run_with_workers(backend, body, []))
    assert backend.calls == 1
    assert len({r.content[0].text for r in results}) == 1


def test_a_call_that_outlasts_the_wait_becomes_a_job_and_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn gets a job id; the session gets `job_completed` when the answer lands."""
    gate = threading.Event()
    backend = _Backend(gate=gate)
    notified: list[Any] = []
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "chemclaw.connectors.queued.record_job_started",
        lambda job_id, kind: started.append((job_id, kind)),
    )

    async def body(client: Any) -> CallToolResult:
        _bind_turn(monkeypatch, client)
        result = await dispatch_queued(
            _CONNECTOR, "heavy", {"smiles": "CCCC"}, inline_wait=1, call_timeout=10
        )
        gate.set()
        workflow_id = queued_workflow_id(_CONNECTOR, "heavy", {"smiles": "CCCC"})
        await client.get_workflow_handle(workflow_id).result()
        return result

    result = asyncio.run(_run_with_workers(backend, body, notified))
    workflow_id = queued_workflow_id(_CONNECTOR, "heavy", {"smiles": "CCCC"})
    assert not result.isError
    assert workflow_id in result.content[0].text
    assert started == [(workflow_id, "heavy")]
    assert [(s, k) for s, k, _ in notified] == [("session-a", "job_completed")]
    assert notified[0][2]["job_id"] == workflow_id


def test_a_waiting_call_says_it_is_queued_and_then_that_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card must not read "running" while the call sits in the queue.

    One slot, held by a first call; a second call waits behind it. Its turn reports `queued` (with
    the broker's waiting count, or `None` where the broker cannot say) and then `running` once the
    slot frees, and the answer still comes back as the tool's own result.
    """
    gate = threading.Event()
    # Each call holds its slot a second past the gate, so the waiting call is seen running.
    backend = _Backend(gate=gate, hold=1.0)
    reported: list[tuple[str, str, str, int | None]] = []
    monkeypatch.setattr(
        "chemclaw.connectors.queued.record_tool_queued",
        lambda tool, job_id, state, waiting: reported.append((tool, job_id, state, waiting)),
    )
    monkeypatch.setattr("chemclaw.core.config.settings.queued_tool_progress_seconds", 0.3)
    monkeypatch.setattr("chemclaw.connectors.queued._STATS_UNSUPPORTED", False)
    monkeypatch.setattr("chemclaw.connectors.queued._BACKLOG", {})

    async def body(client: Any) -> tuple[CallToolResult, CallToolResult]:
        _bind_turn(monkeypatch, client)
        first = asyncio.create_task(
            dispatch_queued(_CONNECTOR, "heavy", {"smiles": "C"}, inline_wait=30, call_timeout=10)
        )
        await asyncio.sleep(0.5)
        second = asyncio.create_task(
            dispatch_queued(_CONNECTOR, "heavy", {"smiles": "N"}, inline_wait=30, call_timeout=10)
        )
        await asyncio.sleep(2.0)
        gate.set()
        return await first, await second

    ran, result = asyncio.run(_run_with_workers(backend, body, [], concurrency=1))
    waiting_id = queued_workflow_id(_CONNECTOR, "heavy", {"smiles": "N"})
    states = [state for _tool, job_id, state, _n in reported if job_id == waiting_id]
    assert not result.isError
    # **The model reads the wait too, not only the card.** `tool_queued` goes to the chemist's
    # stream alone, and on the 2026-10-02 lane the model answered "No call waited, queued, or was
    # refused" about two calls that had waited 8 and 14 s. The waiting call's result now says so,
    # after the server's own block, which stays exactly what the server returned.
    texts = [cast(TextContent, block).text for block in result.content]
    assert texts[0] == "heavy:{'smiles': 'N'} for oid-chemist", texts
    assert len(texts) == 2 and "waited about" in texts[1] and repr(_CONNECTOR) in texts[1], texts
    assert not ran.isError
    assert states[:1] == ["queued"], reported
    assert states[-1] == "running", reported
    counts = [n for _t, job_id, state, n in reported if job_id == waiting_id and state == "queued"]
    # The test server reports task-queue stats, so a count must arrive: an always-`None` read
    # would pass a weaker assertion and leave every card without one. A `None` is legitimate only
    # before the worker has scheduled the call's activity (`_progress`'s never-started case).
    numbered = [n for n in counts if n is not None]
    assert numbered and all(isinstance(n, int) and n >= 0 for n in numbered), reported


def test_a_call_on_a_queue_nothing_polls_says_queued_and_never_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live lane started no interactive worker, and every such call's card read "running".

    No worker here at all: the run is accepted and never picked up, so there is no pending
    activity to read. The turn must still say `queued` on its first look — and never `running` —
    and hand back the job id once the wait is spent.
    """
    reported: list[tuple[str, str, str, int | None]] = []
    monkeypatch.setattr(
        "chemclaw.connectors.queued.record_tool_queued",
        lambda tool, job_id, state, waiting: reported.append((tool, job_id, state, waiting)),
    )
    monkeypatch.setattr("chemclaw.connectors.queued.record_job_started", lambda *_: None)
    monkeypatch.setattr("chemclaw.core.config.settings.queued_tool_progress_seconds", 0.3)

    async def body() -> CallToolResult:
        env = await start_local_env_or_skip()
        async with env:
            client = pydantic_client(env)
            _bind_turn(monkeypatch, client)
            return await dispatch_queued(
                _CONNECTOR, "heavy", {"smiles": "O"}, inline_wait=1.5, call_timeout=10
            )

    result = asyncio.run(body())
    waiting_id = queued_workflow_id(_CONNECTOR, "heavy", {"smiles": "O"})
    assert waiting_id in cast(TextContent, result.content[0]).text
    assert [(job_id, state) for _t, job_id, state, _n in reported] == [(waiting_id, "queued")]


class _Description:
    """A `describe()` answer carrying only the pending activities `_progress` reads."""

    def __init__(self, *states: int) -> None:
        self.raw_description = SimpleNamespace(
            pending_activities=[SimpleNamespace(state=s) for s in states]
        )


def test_progress_says_nothing_when_no_activity_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between a finished activity and the closed run, a running call must not read "queued"."""
    from chemclaw.connectors import queued

    handle = SimpleNamespace(id="q", describe=AsyncMock(return_value=_Description()))
    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=AsyncMock()))
    progress = queued._progress(client, handle, _CONNECTOR, 1.0, started=True)  # type: ignore[arg-type]
    assert asyncio.run(progress) is None
    client.workflow_service.describe_task_queue.assert_not_called()


def test_progress_reads_queued_before_any_worker_has_picked_the_run_up() -> None:
    """The other side of the same empty list: never seen running, it is waiting for a worker.

    It answered `None` here too, which on a queue nothing polls meant the card read "running" for
    the whole wait. The count is `None` — the run is not in the activity backlog yet — rather than a
    number that would leave this call out.
    """
    from chemclaw.connectors import queued

    handle = SimpleNamespace(id="q", describe=AsyncMock(return_value=_Description()))
    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=AsyncMock()))
    progress = queued._progress(client, handle, _CONNECTOR, 1.0, started=False)  # type: ignore[arg-type]
    assert asyncio.run(progress) == ("queued", None)
    client.workflow_service.describe_task_queue.assert_not_called()


def test_a_broker_without_stats_is_asked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that answers without stats (1.25.2) is not asked again on every tick."""
    from temporalio.api.enums.v1 import PendingActivityState
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse

    from chemclaw.connectors import queued

    monkeypatch.setattr(queued, "_STATS_UNSUPPORTED", False)
    monkeypatch.setattr(queued, "_BACKLOG", {})
    scheduled = PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
    handle = SimpleNamespace(id="q", describe=AsyncMock(return_value=_Description(scheduled)))
    ask = AsyncMock(return_value=DescribeTaskQueueResponse())
    client = SimpleNamespace(
        namespace="default", workflow_service=SimpleNamespace(describe_task_queue=ask)
    )

    async def twice() -> list[Any]:
        return [await queued._progress(client, handle, _CONNECTOR, 1.0) for _ in range(2)]  # type: ignore[arg-type]

    assert asyncio.run(twice()) == [("queued", None), ("queued", None)]
    assert ask.await_count == 1


def test_a_queued_signal_becomes_a_tool_queued_event() -> None:
    """The signal and the event carry the same four fields — one mapping, no second opinion."""
    from chemclaw.api.events import ToolQueuedEvent
    from chemclaw.api.graph_stream import _signal_event
    from chemclaw.core.turn_signals import ToolQueuedSignal

    event = _signal_event(
        ToolQueuedSignal(tool="predict_pka", job_id="q-1", state="queued", waiting=3)
    )
    assert event == ToolQueuedEvent(tool="predict_pka", job_id="q-1", state="queued", waiting=3)
    assert event.model_dump()["type"] == "tool_queued"
