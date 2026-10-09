"""What the durable tier says about itself: logs, metrics and job records for every outcome.

Each test runs without a broker where it can (an interceptor is an object with one method, a
failure record is a pure function of its input, the cache's branches are reachable from a fake
store), so a change that silences the durable tier turns a test red instead of skipping. Facts that
need Postgres or a live broker say so through `tests/pg.py` and the Temporal fixtures.
"""

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import socket
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.exceptions import ApplicationError
from temporalio.runtime import PrometheusConfig
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ExecuteActivityInput,
    UnsandboxedWorkflowRunner,
    Worker,
)

from chemclaw.connectors.jobs import failed_job_reason
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.logging import ContextFilter
from chemclaw.core.metrics import _HISTOGRAM_BUCKETS, Metrics
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect_options, telemetry_runtime
from chemclaw.durable.connector_job import (
    ConnectorJobInput,
    ConnectorJobWorkflow,
    ended_state,
    failed_job_record,
    failure_reason,
)
from chemclaw.durable.interceptor import (
    ChemclawWorkerInterceptor,
    activities_in_flight,
    activity_context,
    draining,
)
from chemclaw.durable.job_metrics import (
    jobs_in_flight,
    refresh_open_jobs,
)
from chemclaw.durable.job_record import JobRecord, record_job
from chemclaw.durable.job_record_store import PostgresJobRecordSink, read_job_record
from chemclaw.durable.publish import BAD_DATA_RETRY
from chemclaw.durable.publish_results import publish_job_result
from chemclaw.durable.serve import worker_interceptors
from chemclaw.durable.template_activities import (
    AgentStepInput,
    JobStepInput,
    StepIdentity,
    ToolStepInput,
)
from chemclaw.science.calc.store import (
    CalculationKey,
    InMemoryStore,
    ResultPayload,
    cached_compute,
)
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_local_env_or_skip

_JOB = ConnectorJobInput(
    connector="calc",
    job="compare_solvents",
    workflow="XtbJobWorkflow",
    task_queue="connector-calc",
    payload={"smiles": "CCO", "solvents": ["2-methyltetrahydrofuran"]},
    rationale="pick a solvent for the Tuesday batch",
    requested_by="oid-42",
    session_id="sess-7",
    correlation_id="turn-9",
    plan_step="screen three solvents",
    plan_hash="plan-abc",
)


def _input(fn: Any, args: list[Any]) -> ExecuteActivityInput:
    """The SDK's own interceptor input, so the production signature is exercised.

    A hand-rolled stand-in would keep passing if upstream renamed the field the walk reads.
    """
    return ExecuteActivityInput(fn=fn, args=args, executor=None, headers={})


def _env_for(activity_type: str) -> ActivityEnvironment:
    """An activity context whose `activity.info()` names `activity_type`.

    The stock info reports `"unknown"`, and the failure counter's `activity` label must be asserted
    against a real name.
    """
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, activity_type=activity_type)
    return env


class _Terminal(ActivityInboundInterceptor):
    """The end of the interceptor chain: call the function the input names.

    Deliberately does not call `super().__init__`: this *is* the innermost link, so there is no
    `next` to delegate to — which is the one thing the base class's `__init__` exists to store.
    """

    def __init__(self, fn: Any) -> None:
        """Bind the function this terminal invokes."""
        self._fn = fn

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        """No outbound interception; the chain ends here."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Invoke the wrapped function with the input's arguments."""
        return await self._fn(*input.args)


# --------------------------------------------------------------------------------------------
# J3 — the ids the front door stamped reach the worker
# --------------------------------------------------------------------------------------------


def test_an_activity_runs_under_the_ids_its_own_argument_carries() -> None:
    """A worker's log lines carry the ids the activity's argument carries.

    The ids ride in the argument; the interceptor is what binds them, so an operator can join on
    them.
    """
    seen: dict[str, Any] = {}

    @activity.defn(name="observed")
    async def _observed(job: ConnectorJobInput) -> str:
        seen["actor"] = get_current_actor()
        seen["session"] = get_current_session_id()
        seen["correlation"] = get_current_correlation_id()
        return "ok"

    env = ActivityEnvironment()
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_observed))
    asyncio.run(env.run(outer.execute_activity, _input(_observed, [_JOB])))

    assert seen == {"actor": "oid-42", "session": "sess-7", "correlation": "turn-9"}
    # And unbound again — a contextvar left set leaks one run's identity into the next task this
    # worker picks up, which is the failure `template_activities._acting_as` carries a
    # `finally` for.
    assert get_current_actor() is None
    assert get_current_session_id() is None
    assert get_current_correlation_id() is None


def test_an_argument_that_names_no_identity_binds_none() -> None:
    """An activity whose argument names no identity gets none bound.

    A system-triggered activity (the retention sweep, the reindex) has no actor, and must not
    be given one — `require_actor`'s dev fallback and every role gate read what is bound here.
    """
    context = activity_context([{"payload": {"actor": "spoofed"}}, 3, "a string"])
    assert (context.actor, context.session_id, context.correlation_id) == ("", "", "")
    assert context.roles == frozenset()


def test_a_nested_identity_is_read_one_level_down() -> None:
    """A nested identity model is read, one level down.

    The template path carries its ids on `input.identity`, not flat — and that is the path
    whose failures were completely silent (J4).
    """

    class _Identity:
        actor = "oid-7"
        roles = ["process-chemist"]
        session_id = "sess-t"
        correlation_id = "template-run-1"

    class _Step:
        identity = _Identity()

    context = activity_context([_Step()])
    assert context.actor == "oid-7"
    assert context.session_id == "sess-t"
    assert context.correlation_id == "template-run-1"
    # Roles are NOT lifted from the payload — a relayed workflow argument is not a verified role
    # claim, so binding it would let anyone who can enqueue the activity forge a privileged role
    # (security review). The actor crosses for attribution; roles bind empty (fail-closed).
    assert context.roles == frozenset()


def test_the_real_template_step_inputs_are_the_shape_the_walk_reads() -> None:
    """The real template step inputs satisfy the nested-identity walk.

    The test above pins the walk against a stand-in class; this one pins the contract against the
    models, so a renamed field in `StepIdentity` turns it red.
    """
    identity = StepIdentity(
        actor="chemist-1",
        roles=["process-chemist"],
        correlation_id="template-run-1",
        session_id="s-tmpl",
    )
    for step in (
        ToolStepInput(tool="t", arguments={}, identity=identity),
        AgentStepInput(prompt="p", identity=identity),
        JobStepInput(job="j", arguments={}, identity=identity),
    ):
        context = activity_context([step])
        assert (context.actor, context.session_id, context.correlation_id) == (
            "chemist-1",
            "s-tmpl",
            "template-run-1",
        ), type(step).__name__
        assert context.roles == frozenset(), type(step).__name__  # never lifted from the payload


def test_a_model_authored_payload_cannot_supply_an_identity() -> None:
    """A model-authored `payload` cannot supply an identity.

    The walk stops one level down, deliberately: `payload` is exactly the arguments the LLM
    filled in, which is why `ConnectorJobInput` puts the real actor beside it rather than in it.
    """
    spoofed = _JOB.model_copy(update={"payload": {"requested_by": "oid-victim"}})
    assert activity_context([spoofed]).actor == "oid-42"


# --------------------------------------------------------------------------------------------
# J3 — an activity says that it ran, and how it ended
# --------------------------------------------------------------------------------------------


def test_an_activity_logs_a_start_and_a_finish_with_its_temporal_coordinates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """39 of 43 activities logged nothing at all, and `activity.logger` had zero uses in `src/`."""

    @activity.defn(name="observed")
    async def _observed(job: ConnectorJobInput) -> str:
        return "ok"

    env = ActivityEnvironment()
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_observed))
    with caplog.at_level(logging.INFO, logger="chemclaw.durable.interceptor"):
        asyncio.run(env.run(outer.execute_activity, _input(_observed, [_JOB])))

    # `event` is a `log_event` extra rather than a `LogRecord` attribute, so it is read off
    # `__dict__` — which is also the shape a JSON log stack sees it in.
    events = [r.__dict__["event"] for r in caplog.records if "event" in r.__dict__]
    assert events == ["activity.started", "activity.finished"]
    finished = caplog.records[-1]
    assert finished.__dict__["outcome"] == "completed"
    assert finished.__dict__["attempt"] == 1
    # The coordinates that make a line joinable to a run — absent from every worker line before,
    # because the four activities that logged used a plain module logger with no `extra`.
    assert isinstance(finished.__dict__["duration_ms"], float)
    assert {"workflow_id", "run_id", "task_queue"} <= set(finished.__dict__)


def test_an_activity_whose_identity_is_a_bare_argument_is_attributed_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An activity taking identity as plain string arguments is attributed too.

    `_models` skips `str` so a model-authored payload can never supply an identity; activities that
    carry identity beside such a payload are attributed from the function's signature, which a
    payload cannot rename. Asserted through `ContextFilter`, which is what an operator's log lines
    go through.
    """

    @activity.defn(name="flat")
    async def _flat(spec: dict[str, str], actor: str = "", correlation_id: str = "") -> str:
        return "ok"

    env = _env_for("flat")
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_flat))
    context_filter = ContextFilter()
    caplog.handler.addFilter(context_filter)
    try:
        with caplog.at_level(logging.INFO, logger="chemclaw.durable.interceptor"):
            asyncio.run(
                env.run(
                    outer.execute_activity,
                    _input(_flat, [{"smiles": "CCO"}, "oid-chemist-1", "turn-corr-1"]),
                )
            )
    finally:
        caplog.handler.removeFilter(context_filter)

    records = [r for r in caplog.records if "event" in r.__dict__]
    assert [r.__dict__["event"] for r in records] == ["activity.started", "activity.finished"]
    for record in records:
        assert record.__dict__["actor"] == "oid-chemist-1", record.__dict__["event"]
        assert record.__dict__["correlation_id"] == "turn-corr-1", record.__dict__["event"]


def test_a_positional_payload_still_cannot_name_itself_an_actor() -> None:
    """The signature read is by parameter name, so an ordinary payload parameter is not one.

    The pair with the test above: reading arguments by name is only safe while the names come from
    first-party Python, and this is what says the widening stopped where it was argued to.
    """

    @activity.defn(name="payload_only")
    async def _payload_only(requested_by: dict[str, str]) -> str:
        return "ok"

    assert activity_context([{"requested_by": "oid-victim"}], fn=_payload_only).actor == ""


def test_a_failed_attempt_is_counted_and_logged_and_still_propagates() -> None:
    """A failed attempt is counted, logged, and still propagates.

    One row **per attempt**, so a retry storm is a rate rather than a fact only the broker's
    own history holds — and the failure still reaches Temporal, which decides the retry.
    """
    metrics = Metrics()

    async def _doomed(job: ConnectorJobInput) -> str:
        raise ValueError("unknown ALPB solvent '2-methyltetrahydrofuran'")

    env = _env_for("doomed")
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_doomed))
    with _using(metrics), pytest.raises(ValueError):
        asyncio.run(env.run(outer.execute_activity, _input(_doomed, [_JOB])))

    assert 'chemclaw_activity_failures_total{activity="doomed"} 1' in metrics.render()


def test_an_activity_cancelled_by_a_drain_is_counted_as_one() -> None:
    """A cancellation is attributed to the drain only while one is running.

    A drain redelivers work, which is paid for twice; a cancellation outside one is an ordinary
    turn.
    """

    async def _cancelled() -> str:
        raise asyncio.CancelledError

    outside = Metrics()
    with _using(outside), pytest.raises(asyncio.CancelledError):
        asyncio.run(_run_cancelling(_cancelled))
    assert "chemclaw_worker_activities_cancelled_on_drain_total 1" not in outside.render()

    inside = Metrics()
    with _using(inside), draining(), pytest.raises(asyncio.CancelledError):
        asyncio.run(_run_cancelling(_cancelled))
    assert "chemclaw_worker_activities_cancelled_on_drain_total 1" in inside.render()


async def _run_cancelling(fn: Any) -> Any:
    """Drive one activity that is cancelled, as `Worker.shutdown()` cancels an overrunning one."""
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(fn))
    return await _env_for("slow").run(outer.execute_activity, _input(fn, []))


def test_the_in_flight_count_is_visible_while_an_activity_runs() -> None:
    """What the drain log line needed and could not get: the SDK's worker exposes no count."""
    observed: list[int] = []

    @activity.defn(name="counting")
    async def _counting() -> str:
        observed.append(activities_in_flight())
        return "ok"

    env = ActivityEnvironment()
    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_counting))
    asyncio.run(env.run(outer.execute_activity, _input(_counting, [])))
    assert observed == [1]
    assert activities_in_flight() == 0


def test_every_worker_gets_the_same_interceptor_chain() -> None:
    """Every worker is built from the one interceptor chain.

    A third worker wiring two of three cross-cutting concerns is the failure `serve.py` exists
    to prevent; the chain is a function so it cannot be half-copied.
    """
    assert any(isinstance(i, ChemclawWorkerInterceptor) for i in worker_interceptors())


# --------------------------------------------------------------------------------------------
# J1 — a job that fails leaves a row, a counter and a duration
# --------------------------------------------------------------------------------------------


def test_a_failed_run_produces_a_record_carrying_its_reason() -> None:
    """Measured live: two runs, one success and one `ValueError`, left **one** row."""
    record = failed_job_record("job-1", _JOB, "unknown ALPB solvent '2-MeTHF'", 12.5)
    assert record.state == "failed"
    assert record.failure_reason == "unknown ALPB solvent '2-MeTHF'"
    # The launch context travels with it, so the row is as reconstructable as a successful one —
    # which is the whole reason `job_records` exists (D-157).
    assert (record.rationale, record.requested_by, record.correlation_id) == (
        _JOB.rationale,
        _JOB.requested_by,
        _JOB.correlation_id,
    )
    assert record.payload == _JOB.payload
    assert record.runtime_seconds == 12.5
    # `summary` stays empty: it is what a run *produced*, and a listing that cannot tell a result
    # from a failure is the ambiguity the two columns exist to remove.
    assert record.summary == ""


async def test_a_finished_job_moves_a_counter_and_a_duration_in_both_outcomes() -> None:
    """A finished job moves an outcome counter and a duration, either way it ended.

    `chemclaw_jobs_started_total` had no counterpart of any kind, so a connector whose every
    job failed was indistinguishable from an idle one.
    """
    metrics = Metrics()
    completed = JobRecord(
        job_id="job-ok",
        connector="calc",
        job="compare_solvents",
        rationale="r",
        requested_by="oid-42",
        summary="done",
        runtime_seconds=41.0,
    )
    failed = failed_job_record("job-bad", _JOB, "unknown ALPB solvent", 3.0)

    with _using(metrics):
        await record_job(completed)
        await record_job(failed)

    rendered = metrics.render()
    assert 'chemclaw_jobs_finished_total{connector="calc",outcome="completed"} 1' in rendered
    assert 'chemclaw_jobs_finished_total{connector="calc",outcome="failed"} 1' in rendered
    # A distribution, not only the accumulating total: a mean cannot answer "what does a slow one
    # cost", and this is the most expensive work in the system.
    assert 'chemclaw_job_duration_seconds_bucket{connector="calc",le="+Inf"} 2' in rendered


def test_no_workflow_body_can_write_the_in_flight_reading() -> None:
    """The in-flight gauge has no writer a workflow body could call.

    A workflow execution is not "in" a process between tasks, so a body-maintained reading is wrong
    under terminate and eviction. The module exposes a reader and a client-driven refresher only.
    """
    import chemclaw.durable.job_metrics as job_metrics

    assert not hasattr(job_metrics, "job_running")
    assert not hasattr(job_metrics, "job_ended")
    # And the wrapper does not reach for one under another name. `workflow.info()` in the `run`
    # body is fine; in a `finally` it raises `_NotInWorkflowEventLoopError` on shutdown, which is
    # how the old shape announced itself.
    source = inspect.getsource(ConnectorJobWorkflow.run)
    assert "finally:" not in source
    # And the reading names the workflow it counts. A rename would leave the visibility query
    # matching nothing, and a count of zero is the one wrong answer a gauge cannot be caught
    # giving — it looks exactly like an idle deployment.
    assert ConnectorJobWorkflow.__name__ in job_metrics._OPEN_JOBS_QUERY


async def test_a_failed_run_round_trips_through_postgres() -> None:
    """The columns exist and carry the two facts back — the half only a database can prove."""
    await migrated_db_or_skip()
    # A connector name no other test's filter matches: `job_records` is not truncated between tests,
    # and other tests assert exact listings per connector.
    record = failed_job_record(
        "pg-job-failed",
        _JOB.model_copy(update={"connector": "durable-observability-probe"}),
        "unknown ALPB solvent '2-MeTHF'",
        4.5,
    )
    await PostgresJobRecordSink().record(record)
    stored = await read_job_record("pg-job-failed")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.failure_reason == "unknown ALPB solvent '2-MeTHF'"
    assert stored.rationale == _JOB.rationale


def test_an_existing_row_reads_as_completed() -> None:
    """A row written before the column existed reads as `completed`.

    Every row written before the column existed is a completed run — the table could hold
    nothing else — so the default is the truth about them rather than "did not say".
    """
    assert JobRecord(job_id="j", connector="c", job="k", rationale="r", requested_by="a").state == (
        "completed"
    )


# --------------------------------------------------------------------------------------------
# J2 — the SDK's own metrics are wired, and off by default
# --------------------------------------------------------------------------------------------


def test_the_sdk_metrics_runtime_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK's own metrics runtime is opt-in, and one per process.

    Off by default — a process that binds a port nobody asked for is a surprise outside a
    cluster — and present the moment a deployment names one.
    """
    assert "runtime" not in connect_options()

    monkeypatch.setattr("chemclaw.core.temporal_client._RUNTIME", None)
    # Port 0 asks the kernel for a free port: a `Runtime`'s exporter socket has no `close` and is
    # released only on GC, so a fixed port collides on re-entry.
    monkeypatch.setattr(settings, "temporal_metrics_port", 9111)
    monkeypatch.setattr(settings, "temporal_metrics_host", "127.0.0.1")
    # The setting stays non-zero because zero is what *disables* the exporter, and the branch under
    # test is the enabled one; what is redirected is the address it actually binds.
    monkeypatch.setattr(
        "chemclaw.core.temporal_client.PrometheusConfig",
        lambda bind_address: _prometheus_on_a_free_port(),
    )
    # One per process: a `Runtime` owns a Rust core and a bound socket, so a second is either a
    # bind failure or an exposition nobody scrapes.
    assert "runtime" in connect_options()
    assert connect_options()["runtime"] is connect_options()["runtime"]


def _prometheus_on_a_free_port() -> PrometheusConfig:
    """A Prometheus exporter config the kernel picks the port for."""
    return PrometheusConfig(bind_address="127.0.0.1:0")


def test_a_metrics_port_that_cannot_be_bound_degrades_instead_of_failing_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy metrics port is a missing exposition, not an unreachable broker.

    SDK metrics are optional; the worker is not, so the client still connects.
    """
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        monkeypatch.setattr("chemclaw.core.temporal_client._RUNTIME", None)
        monkeypatch.setattr(settings, "temporal_metrics_host", "127.0.0.1")
        monkeypatch.setattr(settings, "temporal_metrics_port", held.getsockname()[1])
        metrics = Metrics()
        with _using(metrics):
            assert telemetry_runtime() is None
            assert "runtime" not in connect_options()
        # And an operator can find out *why* there are no SDK metrics from a scrape rather than
        # from a log search nobody runs.
        assert 'chemclaw_degraded_total{subsystem="temporal_sdk_metrics"}' in metrics.render()


# --------------------------------------------------------------------------------------------
# J10 — the D-011 cache is metered
# --------------------------------------------------------------------------------------------


def _key(name: str) -> CalculationKey:
    """A cache key that does not require a calculator to build."""
    return CalculationKey(
        calc_type="xtb_energy", calc_version="v1", input_hash=name, params_hash="p"
    )


def test_the_cache_separates_a_hit_a_miss_and_a_shared_computation() -> None:
    """The cache separates a hit, a miss, and a single-flighted share.

    A `shared` miss reports `was_cached=True` to its caller, so only a separate outcome shows the
    single-flight working.
    """
    metrics = Metrics()
    store = InMemoryStore()
    started = asyncio.Event()

    async def _run() -> None:
        async def _slow() -> ResultPayload:
            started.set()
            await asyncio.sleep(0.05)
            return {"energy": 1.0}

        async def _never() -> ResultPayload:  # pragma: no cover - a hit must not compute
            raise AssertionError("a stored result was recomputed")

        first = asyncio.create_task(cached_compute(store, _key("a"), _slow))
        await started.wait()
        joined = asyncio.create_task(cached_compute(store, _key("a"), _never))
        await asyncio.gather(first, joined)
        # And now a genuine store hit.
        assert (await cached_compute(store, _key("a"), _never))[1] is True

    with _using(metrics):
        asyncio.run(_run())

    rendered = metrics.render()
    assert 'chemclaw_calc_cache_total{outcome="miss"} 1' in rendered
    assert 'chemclaw_calc_cache_total{outcome="shared"} 1' in rendered
    assert 'chemclaw_calc_cache_total{outcome="hit"} 1' in rendered
    # The seconds saved: a count cannot tell a thousand avoided lookups from one avoided hour-long
    # search. Asserted as a lower bound (a loaded machine only overshoots) and as twice `_slow`'s
    # sleep, because the shared waiter saved a whole computation as the later hit did.
    saved = metrics.value("chemclaw_calc_cache_seconds_saved_total")
    assert saved >= 0.10, (
        f"two avoided computations of ~0.05 s each credited {saved:.4f} s — a cache hit is "
        "counted and never valued"
    )


# --------------------------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _using(metrics: Metrics) -> Iterator[Metrics]:
    """Point both registry readers at a fresh `Metrics` for the body of a test.

    The process registry is a module singleton. Two patch points: a counter goes through
    `record_metric`, while a gauge is bound onto the registry object directly.
    """
    with (
        mock.patch("chemclaw.core.metrics_bridge.METRICS", metrics),
        mock.patch("chemclaw.durable.job_metrics.METRICS", metrics),
    ):
        yield metrics


# Tests against a real-time dev server: each defect below is a wall-clock worker event that no
# in-process stand-in reproduces.


_CHILD_QUEUE_NOBODY_SERVES = "connector-nobody-serves-this"


def _hanging_job(**overrides: Any) -> ConnectorJobInput:
    """A job whose child is started on a queue no worker polls, so the parent stays RUNNING.

    The cheapest honest way to hold a parent open, and exactly what the wrapper does during a long
    search.
    """
    return _JOB.model_copy(
        update={"task_queue": _CHILD_QUEUE_NOBODY_SERVES, "workflow": "NeverServed", **overrides}
    )


@contextlib.asynccontextmanager
async def _core_worker(client: Any, **kwargs: Any) -> Any:
    """The background worker a deployment runs, with the record sink stubbed out."""
    from chemclaw.durable.memory_jobs import publish_memory_note_activity
    from chemclaw.durable.notify import record_session_event_activity

    worker = Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=[ConnectorJobWorkflow],
        activities=[publish_memory_note_activity, record_session_event_activity, record_job],
        **kwargs,
    )
    async with worker:
        yield worker


def test_the_in_flight_gauge_survives_terminate_and_eviction() -> None:
    """The in-flight reading is the broker's, so terminate and eviction leave it correct.

    A terminated workflow never runs its `finally`, and an evicted one is still RUNNING.
    """

    async def _run() -> None:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            # Evicted after every workflow task: the posture that used to read zero.
            async with _core_worker(client, max_cached_workflows=0):
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    _hanging_job(),
                    id="in-flight-probe",
                    task_queue=settings.background_task_queue,
                )
                await _until_running(handle)
                await _until_in_flight(client, 1.0)
                await handle.terminate()
                await _until_not_running(handle)
                await _until_in_flight(client, 0.0)

    asyncio.run(_run())


def test_a_status_poll_with_no_wait_does_not_block_on_a_running_job() -> None:
    """`wait_seconds=0`, the front door's normal path, returns promptly for a running job.

    `handle.result()` long-polls a running execution, so it must not be reached without a wait.
    """
    from chemclaw.agent import durable_tools

    async def _run() -> tuple[str, float]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with _core_worker(client):
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    _hanging_job(),
                    id="no-wait-probe",
                    task_queue=settings.background_task_queue,
                )
                await _until_running(handle)
                with mock.patch.object(durable_tools, "connect", _returning(client)):
                    started = time.perf_counter()
                    # Bounded so a regression *fails* rather than hangs: without the guard this
                    # call does not return until the job does, and an unbounded await here would
                    # wedge the suite instead of reporting the defect.
                    status = await asyncio.wait_for(
                        durable_tools.job_status(handle.id, wait_seconds=0.0), 15.0
                    )
                    elapsed = time.perf_counter() - started
                await handle.terminate()
                return status.status, elapsed

    status, elapsed = asyncio.run(_run())
    assert status == "running"
    # A generous bound: the point is "one round trip", not a latency budget. The defect took the
    # whole life of the job.
    assert elapsed < 5.0


def test_a_cancellation_is_recorded_as_cancelled_and_anything_else_as_failed() -> None:
    """`ended_state` reads the SDK's own cancellation predicate, wrapped or bare."""
    from temporalio.exceptions import CancelledError

    assert ended_state(asyncio.CancelledError()) == "cancelled"
    assert ended_state(CancelledError("Cancelled")) == "cancelled"
    assert ended_state(ValueError("unknown ALPB solvent")) == "failed"
    record = failed_job_record("job-c", _JOB, "Cancelled", 1.0, state="cancelled")
    assert (record.state, record.failure_reason) == ("cancelled", "Cancelled")


@workflow.defn(name="CancelProbeChild")
class _CancelProbeChild:
    """A connector child that is served and never finishes, so only a cancellation ends it.

    Served rather than parked on an unpolled queue (`_hanging_job`), because the wrapper waits for
    its child to *acknowledge* a cancellation, and a child no worker runs never does.
    """

    @workflow.run
    async def run(self, _payload: Any) -> None:
        """Wait for a condition that never comes."""
        await workflow.wait_condition(lambda: False)


def test_a_cancelled_job_is_listed_as_cancelled_not_failed() -> None:
    """The registry listing and the job's own status agree on a run stopped by a person.

    Once Temporal's history ages out the record is the only answer left, so it must record
    `cancelled` as the broker did. Driven against a real dev server, delivering the cancellation as
    `cancel_durable_job` does.
    """
    from chemclaw.agent import durable_tools

    recorded: list[JobRecord] = []

    class _CapturingSink:
        async def record(self, record: JobRecord) -> None:
            recorded.append(record)

    async def _lookup(job_id: str) -> JobRecord | None:
        return next((r for r in recorded if r.job_id == job_id), None)

    async def _run() -> tuple[str, str, str]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            with mock.patch("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink):
                child = Worker(
                    client,
                    task_queue="cancel-probe-child",
                    workflows=[_CancelProbeChild],
                    workflow_runner=UnsandboxedWorkflowRunner(),
                )
                async with _core_worker(client), child:
                    handle = await client.start_workflow(
                        ConnectorJobWorkflow.run,
                        _JOB.model_copy(
                            update={
                                "workflow": "CancelProbeChild",
                                "task_queue": "cancel-probe-child",
                                "session_id": "",
                            }
                        ),
                        id="cancel-record-probe",
                        task_queue=settings.background_task_queue,
                    )
                    await _until_running(handle)
                    await handle.cancel()
                    description = await _until_not_running(handle, timeout=60.0)
                    with mock.patch.object(durable_tools, "connect", _returning(client)):
                        live = await durable_tools.job_status(handle.id, wait_seconds=0.0)
            # The detail once the broker has forgotten the run: the stored record, read back.
            with mock.patch.object(durable_tools, "lookup_job_record", _lookup):
                stored = await durable_tools._recorded_status("cancel-record-probe")
            assert stored is not None
            return description.status.name, live.status, stored.status

    broker, detail, listed = asyncio.run(_run())
    assert broker == "CANCELED"
    assert detail == "cancelled"
    assert listed == detail
    assert [record.state for record in recorded] == ["cancelled"]


def test_a_failed_job_reaches_its_session_even_with_the_record_queue_unserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure record is bounded, so it cannot hold a dead job open ahead of the push-back.

    The record step carries a `schedule_to_start_timeout`, so an unserved queue cannot delay the
    failure notice indefinitely. Budgets are shortened so the test measures the bound rather than
    waiting out the shipped one.
    """
    from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow

    monkeypatch.setattr(settings, "background_task_queue", "nobody-serves-this-either")
    monkeypatch.setattr(settings, "job_record_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "activity_timeout_seconds", 2.0)
    # The queue-wait half of the bound, which is what actually fires here: nothing serves that
    # queue, so the record never starts and the run has to give up on it rather than on the work.
    monkeypatch.setattr(settings, "template_step_timeout_seconds", 2.0)

    async def _run() -> Any:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            wrapper = Worker(
                client, task_queue="wrapper-only", workflows=[ConnectorJobWorkflow], activities=[]
            )
            bundle = Worker(client, task_queue="connector-fixture", workflows=[FixtureJobWorkflow])
            async with wrapper, bundle:
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    _JOB.model_copy(
                        update={
                            "workflow": "FixtureJobWorkflow",
                            "task_queue": "connector-fixture",
                            "payload": {"subject": "boom"},
                        }
                    ),
                    id="unserved-record-probe",
                    task_queue="wrapper-only",
                )
                return await _until_not_running(handle, timeout=60.0)

    # It ends, and it ends *failed* rather than by being abandoned. Before the bound existed it did
    # not end at all: the assertion is the absence of the hang, and `_until_not_running` raising is
    # what a regression looks like.
    assert asyncio.run(_run()).status.name == "FAILED"


def test_a_worker_runs_exactly_one_tracing_interceptor() -> None:
    """A worker runs exactly one tracing interceptor.

    A `Worker` prepends the client's interceptors, so adding ours again would trace everything
    twice.
    """

    async def _run() -> list[str]:
        async with await start_local_env_or_skip() as env:
            config = env.client.config()
            config["interceptors"] = [TracingInterceptor()]
            client = Client(**config)
            async with _core_worker(client, interceptors=worker_interceptors()) as worker:
                chain = worker._activity_worker._interceptors
                return [type(i).__name__ for i in chain]

    merged = asyncio.run(_run())
    assert merged.count("TracingInterceptor") == 1
    # And ours runs *inside* the client's tracing interceptor, which is the right way round: a span
    # that does not enclose the log line and the failure counter it explains ends too early.
    assert merged == ["TracingInterceptor", "ChemclawWorkerInterceptor"]


def _returning(value: Any) -> Any:
    """An async callable that ignores its arguments and returns `value`."""

    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _call


async def _until_running(handle: Any, timeout: float = 20.0) -> None:
    """Wait until the broker reports this execution as RUNNING with its first task done."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        description = await handle.describe()
        if description.status is not None and description.status.name == "RUNNING":
            # RUNNING is true from the moment it is started; give the worker its first task so the
            # child has actually been scheduled and the wrapper is genuinely waiting.
            await asyncio.sleep(0.5)
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the probe workflow never reached RUNNING")


async def _until_not_running(handle: Any, timeout: float = 20.0) -> Any:
    """Wait until the broker reports this execution as closed, returning its final description."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        description = await handle.describe()
        if description.status is not None and description.status.name != "RUNNING":
            return description
        await asyncio.sleep(0.1)
    raise AssertionError("the probe workflow never left RUNNING")


async def _until_in_flight(client: Any, expected: float, timeout: float = 20.0) -> None:
    """Refresh the gauge until it reads `expected`, naming the last reading if it never does.

    `describe()` reads the execution's own state; the gauge reads the visibility store, which the
    broker updates asynchronously after it. The gauge follows the broker's visibility by design, so
    a test of it waits for that condition rather than for the execution's.
    """
    deadline = time.monotonic() + timeout
    reading = jobs_in_flight()
    while time.monotonic() < deadline:
        await refresh_open_jobs(client)
        reading = jobs_in_flight()
        if reading == expected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the gauge never read {expected}; its last reading was {reading}")


async def test_a_failure_record_never_erases_a_finished_run_s_result() -> None:
    """A failure record never erases a finished run's result.

    `failed_job_record` supplies none of the columns that hold what a run produced, so the upsert
    must keep them. Reachable when `record_job` commits and the activity then overruns its timeout.
    """
    await migrated_db_or_skip()
    probe = _JOB.model_copy(update={"connector": "durable-observability-probe"})
    finished = JobRecord(
        job_id="pg-job-not-erased",
        connector=probe.connector,
        job=probe.job,
        rationale=probe.rationale,
        requested_by=probe.requested_by,
        summary="dG = -12.3 kJ/mol",
        result={"dg_kj_per_mol": -12.3},
        note_id="note-1",
        calc_refs=["k1", "k2"],
        payload_kind="SolventScreen",
        runtime_seconds=9.0,
    )
    sink = PostgresJobRecordSink()
    await sink.record(finished)
    await sink.record(failed_job_record("pg-job-not-erased", probe, "Cancelled", 9.1))
    stored = await read_job_record("pg-job-not-erased")
    assert stored is not None
    # How it ended is refreshed…
    assert (stored.state, stored.failure_reason) == ("failed", "Cancelled")
    # …and what it produced is not touched, because a failure record has nothing to say about
    # a result and must not say it loudly enough to erase one.
    assert stored.summary == "dG = -12.3 kJ/mol"
    assert stored.result == {"dg_kj_per_mol": -12.3}
    assert stored.note_id == "note-1"
    assert stored.calc_refs == ["k1", "k2"]
    assert stored.payload_kind == "SolventScreen"
    # And the reverse still replaces the row entire: a failed run that is re-run and succeeds
    # is the case the whole-row upsert exists for (D-011 lets only a failed id re-execute).
    await sink.record(finished.model_copy(update={"summary": "second run"}))
    again = await read_job_record("pg-job-not-erased")
    assert again is not None
    assert (again.state, again.summary, again.failure_reason) == ("completed", "second run", "")


def test_a_run_that_fails_after_recording_is_not_recorded_a_second_time() -> None:
    """One run, one `chemclaw_jobs_finished_total` and one duration sample, however it ended.

    A best-effort step raising something other than `ActivityError` after the completed record must
    not write a second, failed record. Driven unsandboxed so that step can raise as a real one does.
    """
    recorded: list[JobRecord] = []

    class _CapturingSink:
        async def record(self, record: JobRecord) -> None:
            recorded.append(record)

    async def _explode(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("the note could not be stamped with its run provenance")

    async def _run() -> Any:
        from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow

        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            with (
                mock.patch("chemclaw.durable.job_record.default_job_record_sink", _CapturingSink),
                mock.patch("chemclaw.durable.connector_job.publish_note_best_effort", _explode),
            ):
                wrapper = Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    workflows=[ConnectorJobWorkflow],
                    activities=[record_job, publish_job_result],
                    workflow_runner=UnsandboxedWorkflowRunner(),
                )
                bundle = Worker(
                    client, task_queue="connector-fixture", workflows=[FixtureJobWorkflow]
                )
                async with wrapper, bundle:
                    handle = await client.start_workflow(
                        ConnectorJobWorkflow.run,
                        _JOB.model_copy(
                            update={
                                "workflow": "FixtureJobWorkflow",
                                "task_queue": "connector-fixture",
                                "payload": {"subject": "benzene"},
                                "publish_to_graph": True,
                                "session_id": "",
                            }
                        ),
                        id="record-once-probe",
                        task_queue=settings.background_task_queue,
                    )
                    return await _until_not_running(handle, timeout=60.0)

    description = asyncio.run(_run())
    # The run genuinely ended badly — the point is what it wrote, not that it survived.
    assert description.status.name == "FAILED"
    # Exactly one record, and it is the one that carries the science.
    assert [record.state for record in recorded] == ["completed"]
    assert recorded[0].summary == "fixture job ran on benzene"


def test_a_cancelled_activity_is_not_an_activity_failure() -> None:
    """A cancelled activity is not an activity failure.

    A drain cancels activities, and `ChemclawActivityRetryStorm` reads the failure series as "every
    attempt is failing".
    """

    @activity.defn(name="slow")
    async def _cancelled() -> str:
        raise asyncio.CancelledError

    metrics = Metrics()
    with _using(metrics), draining(), pytest.raises(asyncio.CancelledError):
        asyncio.run(_run_cancelling(_cancelled))
    rendered = metrics.render()
    assert 'chemclaw_activity_failures_total{activity="slow"}' not in rendered
    # The cancellation is still counted — on the series that means cancellation.
    assert "chemclaw_worker_activities_cancelled_on_drain_total 1" in rendered

    # And a real failure still books one, so the exclusion is a narrowing rather than a silencing.
    @activity.defn(name="broken")
    async def _broken() -> str:
        raise ValueError("no")

    failures = Metrics()
    with _using(failures), pytest.raises(ValueError):
        env = _env_for("broken")
        outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_broken))
        asyncio.run(env.run(outer.execute_activity, _input(_broken, [])))
    assert 'chemclaw_activity_failures_total{activity="broken"} 1' in failures.render()


def test_a_failure_before_the_activity_leaks_no_count() -> None:
    """A failure before the activity body leaks no in-flight count.

    The `finally` must cover the statements whose effects it undoes. Asserted on
    `activities_in_flight()`, which is module state; contextvars set inside
    `ActivityEnvironment.run` never escape it, so asserting them would pass by construction.
    """

    @activity.defn(name="never-reached")
    async def _never() -> str:  # pragma: no cover - the interceptor raises first
        raise AssertionError("the activity should not have been reached")

    outer = ChemclawWorkerInterceptor().intercept_activity(_Terminal(_never))
    with (
        mock.patch("chemclaw.durable.interceptor.log_event", side_effect=RuntimeError("disk full")),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(_env_for("never-reached").run(outer.execute_activity, _input(_never, [_JOB])))
    assert activities_in_flight() == 0


def test_a_failure_reason_is_bounded_before_it_reaches_a_column_and_a_turn() -> None:
    """A failure reason is bounded before it reaches a column and a model turn.

    `str(cause)` has no length, and the column, the push-back payload, its dedupe key and the status
    summary downstream have no cap either.
    """
    reason = failure_reason(ValueError("x" * 5000))
    assert len(reason) == 500
    assert reason == "x" * 500


def test_a_transport_fault_is_not_a_job_s_failure_reason() -> None:
    """A transport fault during a poll is not the job's failure reason.

    Temporal raises `WorkflowFailureError` for every bad ending, so a broad clause beneath it
    catches only transport errors, which say nothing about the run.
    """

    class _Handle:
        async def result(self) -> None:
            raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(RPCError):
        asyncio.run(failed_job_reason(_Handle()))


def test_the_job_duration_histogram_brackets_the_job_ceiling() -> None:
    """The job duration histogram brackets the job ceiling.

    `histogram_quantile` returns the highest finite boundary rather than interpolating into `+Inf`,
    so a top bucket below the timeout saturates the p95. Asserted against the setting, not the
    buckets.
    """
    buckets = _HISTOGRAM_BUCKETS["chemclaw_job_duration_seconds"]
    ceiling = settings.connector_job_timeout_seconds
    assert max(buckets) > ceiling, "nothing above the ceiling means a saturating quantile"
    assert any(b < ceiling for b in buckets), "the ceiling must be bracketed on both sides"
    # And the longest single activity a job can contain lands below the top boundary rather than
    # in `+Inf`.
    assert max(buckets) > settings.xtb_job_timeout_seconds

    metrics = Metrics()
    metrics.observe("chemclaw_job_duration_seconds", 15000.0, {"connector": "calc"})
    rendered = metrics.render()
    assert 'chemclaw_job_duration_seconds_bucket{connector="calc",le="21600"} 1' in rendered


@activity.defn(name="w8b_result_size")
async def _sized_result(payload_bytes: int) -> str:
    """An activity whose result size the caller chooses — the subject of the two tests below."""
    return "x" * payload_bytes


@workflow.defn(name="W8bResultSize", sandboxed=False)
class _ResultSizeWorkflow:
    """Run `_sized_result` once and hand back its length: what the broker actually kept."""

    @workflow.run
    async def run(self, payload_bytes: int) -> int:
        return len(
            await workflow.execute_activity(
                "w8b_result_size",
                payload_bytes,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=BAD_DATA_RETRY,
            )
        )


@pytest.mark.timeout(300)
def test_a_result_the_broker_would_refuse_is_a_counted_failing_activity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A result over the broker's blob limit fails the activity, on the record, in every consumer.

    Driven against a real broker: the SDK completes the activity outside the interceptor's `try`, so
    without a pre-check the refusal happens after every first-party report. Asserted: the failure
    counter by `activity` label (how `ChemclawActivityRetryStorm` groups), the logged `outcome`, and
    the workflow reaching a terminal failure. The under-ceiling arm shows this is a ceiling, not a
    ban.
    """
    ceiling = settings.activity_result_max_bytes
    # Over the *server's* limit as well as ours, so "the broker would refuse it" is a fact about
    # this payload rather than a claim about the setting. The ceiling ships at 2 MiB, which is
    # `limit.blobSize.error`'s own default.
    refused_bytes = ceiling + 1_000_000
    metrics = Metrics()

    async def _drive() -> tuple[int, BaseException | None, object]:
        async with await start_local_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="w8b-result-size",
                workflows=[_ResultSizeWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[_sized_result],
                interceptors=worker_interceptors(),
            ):
                kept = await client.execute_workflow(
                    _ResultSizeWorkflow.run,
                    ceiling // 2,
                    id="w8b-under-the-ceiling",
                    task_queue="w8b-result-size",
                )
                handle = await client.start_workflow(
                    _ResultSizeWorkflow.run,
                    refused_bytes,
                    id="w8b-over-the-ceiling",
                    task_queue="w8b-result-size",
                )
                with pytest.raises(WorkflowFailureError) as refused:
                    await handle.result()
                # The `ActivityError` wrapper says only "Activity task failed"; the name Temporal
                # matched against `non_retryable_error_types` is on the `ApplicationError` under it.
                activity_error = refused.value.cause
                # `BaseException.cause` is not a typed attribute; the chain here is
                # `WorkflowFailureError -> ActivityError -> ApplicationError`, and only the last
                # carries the `type` Temporal matched against `non_retryable_error_types`.
                under = getattr(activity_error, "cause", None)
                return (
                    kept,
                    under,
                    (await handle.describe()).status,
                )

    with _using(metrics), caplog.at_level(logging.INFO, logger="chemclaw.durable.interceptor"):
        kept, application_error, status = asyncio.run(_drive())

    assert kept == ceiling // 2, (
        "the under-ceiling result did not survive the round trip, so this test is not measuring a "
        "ceiling"
    )
    assert status == WorkflowExecutionStatus.FAILED, (
        f"the refused result left the workflow {status}; a 6 MB result used to leave it RUNNING "
        "until the caller gave up, which is the half no metric and no log line reported"
    )
    assert isinstance(application_error, ApplicationError), (
        f"the activity failed as {application_error!r} rather than as an application error, so "
        "nothing carries the type Temporal classifies on"
    )
    assert application_error.type == "ActivityResultTooLarge", (
        f"the workflow failed for some other reason ({application_error.type}), so the refusal is "
        "not what it reports and an operator still cannot name the fault"
    )

    rendered = metrics.render()
    assert 'chemclaw_activity_failures_total{activity="w8b_result_size"} 1' in rendered, (
        "`ChemclawActivityRetryStorm` reads `sum by (activity) "
        "(rate(chemclaw_activity_failures_total[15m]))`; an unlabelled or absent sample is the "
        f"flat series the alert cannot fire on. Got:\n{rendered}"
    )

    finished = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "activity.finished"
        and record.__dict__.get("activity") == "w8b_result_size"
    ]
    outcomes = [record.__dict__["outcome"] for record in finished]
    assert outcomes.count("failed") == 1, (
        f"the refused attempt's own line says {outcomes}; it said `completed` for every attempt "
        "before the pre-check, which is the sentence that sent an operator looking for a network "
        "fault"
    )
    # Exactly one attempt: the result is deterministic, so `ActivityResultTooLarge` must be in
    # `durable/publish._BAD_DATA_TYPES` or the retry budget is burnt re-serializing the same bytes.
    # The attempt count is where the server-applied policy is observable.
    assert outcomes.count("completed") == 1, (
        f"the kept result's line should still say completed, got {outcomes}"
    )
