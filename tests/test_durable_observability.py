"""What the durable tier says about itself — measured, because the answer used to be "nothing".

The measurement this file pins down was taken against a live broker on 2026-08-27: one
`ConnectorJobWorkflow` run twice, once succeeding and once failing on a `ValueError`. The
successful job emitted **zero** log records. The failed job emitted zero first-party records and
moved no metric. `job_records` held one row for two jobs — the failed one had none. The only output
either run produced was two `temporalio` SDK warnings.

So each test here asserts one half of that being false now, and every one of them is written to run
**without a broker**: an interceptor is an object with one method, a workflow's failure record is a
pure function of its input, and the cache's three branches are reachable from a fake store. That
matters because the property being protected is not "this works on a good day" — it is "a change
that silences the durable tier turns a test red", and a test that needs Temporal running is a test
that skips exactly where it is needed.

The two facts that genuinely need Postgres (the `state` / `failure_reason` columns round-tripping)
say so through `tests/pg.py::migrated_db_or_skip`, which the run's own epilogue counts.
"""

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import socket
import time
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
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
    """The SDK's own interceptor input, so what is exercised is the production signature.

    A hand-rolled stand-in would type-check as `Any` and would keep passing if upstream renamed the
    field the walk reads — which is the coupling `tests/test_upstream_surface.py` exists to make
    loud rather than silent.
    """
    return ExecuteActivityInput(fn=fn, args=args, executor=None, headers={})


def _env_for(activity_type: str) -> ActivityEnvironment:
    """An activity context whose `activity.info()` names `activity_type`.

    `ActivityEnvironment`'s stock info reports `activity_type="unknown"`, and the whole point of
    `chemclaw_activity_failures_total{activity=...}` is that an operator can see *which* activity
    is failing — so the label has to be asserted against a real name.
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
    """`set_current_correlation_id` had exactly one caller in the tree: the front door.

    So every log line every worker wrote rendered `correlation_id="-" actor="-" session_id="-"`,
    while `deploy/README.md` told an operator to join on those fields. The ids were never missing —
    they ride in the activity's argument — and nothing bound them.
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
    assert context.roles == frozenset({"process-chemist"})


def test_the_real_template_step_inputs_are_the_shape_the_walk_reads() -> None:
    """The nested-identity walk is asserted above against a stand-in class; this asserts the models.

    Both matter and neither substitutes for the other. The test above pins the *walk* — that a
    nested `identity` is read one level down — and it would keep passing if `StepIdentity` renamed
    `correlation_id` tomorrow, because it declares its own shape. This one pins the *contract*:
    that the three step inputs a real template run carries actually satisfy that walk.

    It is the assertion that lets `template_activities._acting_as` be described as redundant on a
    worker rather than merely believed to be, and it is what would go red if the two ever drifted —
    which is the only way the tree ends up with two producers that disagree instead of two that
    cannot.
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
        assert context.roles == frozenset({"process-chemist"}), type(step).__name__


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
    """Four activities take identity as plain strings, and their own records were anonymous.

    `_models` skips `str` outright — deliberately, so a model-authored payload can never supply an
    identity — and four activities in this tree carry theirs beside such a payload rather than
    inside a model: `connectors/calc/activities.py::run_xtb_calculation`,
    `connectors/bo/activities.py::record_campaign_run`,
    `durable/memory_jobs.py::publish_memory_note_activity` and
    `durable/report_workflow.py::propose_report`. None is rescued by the model walk, so both
    interceptor records rendered `actor=- correlation_id=-` for the fleet's longest-running
    activity while its own dispatch ran fully attributed — and `deploy/README.md` tells an operator
    to grep those fields.

    The names come from the activity function's **signature**, which is first-party Python, so the
    property `test_a_model_authored_payload_cannot_supply_an_identity` pins is untouched: `spec`
    is not one of the four names, and a payload cannot rename the parameter it is bound to.

    Asserted through `ContextFilter`, because the record's own `extra` never carried these and the
    filter is what an operator's log lines actually go through.
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

    `durable/serve.py`'s docstring names the cost — work redelivered and therefore paid for
    twice — and nothing measured it. Counted only *during* a drain, because a cancellation
    outside one is an ordinary cancelled turn, which nobody is paying for twice.
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


def test_a_finished_job_moves_a_counter_and_a_duration_in_both_outcomes() -> None:
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

    async def _run() -> None:
        with _using(metrics):
            await record_job(completed)
            await record_job(failed)

    asyncio.run(_run())
    rendered = metrics.render()
    assert 'chemclaw_jobs_finished_total{connector="calc",outcome="completed"} 1' in rendered
    assert 'chemclaw_jobs_finished_total{connector="calc",outcome="failed"} 1' in rendered
    # A distribution, not only the accumulating total: a mean cannot answer "what does a slow one
    # cost", and this is the most expensive work in the system.
    assert 'chemclaw_job_duration_seconds_bucket{connector="calc",le="+Inf"} 2' in rendered


def test_no_workflow_body_can_write_the_in_flight_reading() -> None:
    """The gauge has no writer a workflow body could call — an absence test, deliberately.

    `chemclaw_jobs_in_flight` used to be a process-local `set` that `ConnectorJobWorkflow.run`
    added itself to and discarded in its `finally`, on the stated ground that neither call needed
    an `is_replaying` guard "because this is a statement about the present". Driven against a live
    broker it was wrong in three directions and raised in a fourth (see `durable/job_metrics.py`),
    because a workflow execution is not "in" a process at all between tasks.

    So this fails whoever restores that shape: the module exposes a *reader* and a *refresher* that
    takes a client, and nothing a workflow body can reach. The same guard
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` left behind for
    `audit_events.agent`, for the same reason — a claim with no honest producer.
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


def test_a_failed_run_round_trips_through_postgres() -> None:
    """The columns exist and carry the two facts back — the half only a database can prove."""

    async def _run() -> None:
        await migrated_db_or_skip()
        # A connector name no other test's filter can match. `job_records` is not truncated
        # between tests, and `test_job_record_postgres.py` asserts an *exact* listing for
        # `connector="calc"` — so a row this file leaves behind under a shared name is a failure
        # in somebody else's test, which is the worst kind to debug.
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

    asyncio.run(_run())


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
    # **Port 0, not a fixed one.** This used to name 39100 and leave the listener behind: a
    # `Runtime` owns a Rust-side socket with no `close`, `monkeypatch` restores only the module
    # attribute, and the exporter is released on GC rather than deterministically. Re-entering the
    # test in one process — or anything else reaching for 39100 — then met
    # `ValueError: Failed starting Prometheus exporter: Address already in use`, which is finding
    # 7's fault reproduced by the test written to check finding 7's feature. Port 0 asks the
    # kernel for a free one every time, which is the only form of this that cannot collide.
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

    Measured on 2026-08-28 with 127.0.0.1:9111 already held: `Runtime(...)` raised
    `ValueError: Failed starting Prometheus exporter: Address already in use` from inside
    `connect_options()`, and `connect()`'s `except Exception` reported it as "the durable execution
    backend (Temporal) is unreachable … This is an infrastructure outage" — for a broker that was
    up and answering. SDK metrics are optional; the worker is not.
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

    `was_cached` reached one per-job field and never a number, so the largest cost lever in
    the system was observable only under DEBUG on the hottest read there is.

    Three outcomes, not two: a `shared` miss reports `was_cached=True` to its caller, so on the
    boolean it was indistinguishable from a hit — and it is the single-flight working, which is
    exactly what anyone asking "is the cache earning its keep" wants separated.
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


# --------------------------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _using(metrics: Metrics) -> Iterator[Metrics]:
    """Point both registry readers at a fresh `Metrics` for the body of a test.

    The process registry is a module singleton, so asserting on it directly would make every test
    in this file order-dependent.

    **Two patch points, not one**, and the difference is what a gauge is: a counter goes through
    `record_metric`'s swallow, while a gauge is *bound* onto the registry object directly
    (`METRICS.bind_gauge`) — which is exactly what stops a gauge drifting from its source, and
    exactly why patching only the bridge would leave the gauge on the process registry.
    """
    with (
        mock.patch("chemclaw.core.metrics_bridge.METRICS", metrics),
        mock.patch("chemclaw.durable.job_metrics.METRICS", metrics),
    ):
        yield metrics


# --------------------------------------------------------------------------------------------
# 2026-08-28 — the durable-tier review: what only a running broker could show
# --------------------------------------------------------------------------------------------
#
# Every test below drives a real `ConnectorJobWorkflow` against a **real-time** dev server, because
# each of the four defects they pin is a wall-clock worker event that no in-process stand-in
# reproduced: the previous suite called `job_running`/`job_ended` directly and never drove a
# workflow, which is exactly why a gauge that was wrong in three directions and raised in a fourth
# passed for as long as it existed.


_CHILD_QUEUE_NOBODY_SERVES = "connector-nobody-serves-this"


def _hanging_job(**overrides: Any) -> ConnectorJobInput:
    """A job whose child is started on a queue no worker polls, so the parent stays RUNNING.

    A child that is *scheduled and never picked up* is the cheapest honest way to hold a parent
    open for as long as a test needs, and it needs no second workflow class: the wrapper is doing
    exactly what it does while a CREST search runs.
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
    """The reading is the broker's, so the two events that broke the old one leave it correct.

    Measured on 2026-08-28 against a live broker, with the reading kept by the workflow body:
    a terminate left `chemclaw_jobs_in_flight` at `1.0` for the life of the process (a termination
    never resumes workflow code, so the `finally` never ran), and an eviction — the shipped
    `max_cached_workflows=0` posture — read `0.0` while the workflow was still `RUNNING`, which is
    the reading it must not give for exactly the long idle parents it exists to count.
    """

    async def _run() -> list[float]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            readings: list[float] = []
            # Evicted after every workflow task: the posture that used to read zero.
            async with _core_worker(client, max_cached_workflows=0):
                handle = await client.start_workflow(
                    ConnectorJobWorkflow.run,
                    _hanging_job(),
                    id="in-flight-probe",
                    task_queue=settings.background_task_queue,
                )
                await _until_running(handle)
                await refresh_open_jobs(client)
                readings.append(jobs_in_flight())
                await handle.terminate()
                await _until_not_running(handle)
                await refresh_open_jobs(client)
                readings.append(jobs_in_flight())
            return readings

    running, terminated = asyncio.run(_run())
    assert running == 1.0
    assert terminated == 0.0


def test_a_status_poll_with_no_wait_does_not_block_on_a_running_job() -> None:
    """`wait_seconds=0` is the front door's normal path, and it used to be an unbounded long-poll.

    `api/routes/jobs.py` passes nothing, so `job_status` fell through to `failed_job_reason`, whose
    `handle.result()` is a history read on a *closed* execution and Temporal's long-poll on a
    running one. Measured live: `job_status(wait_seconds=0)` blocked for over 15 s on a RUNNING
    workflow and would have blocked for the life of the job.
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


def test_a_failed_job_reaches_its_session_even_with_the_record_queue_unserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure record is bounded, so it cannot hold a dead job open ahead of the push-back.

    `_record_run` carried `start_to_close_timeout` alone, which only starts once a worker has
    *picked the task up*. Measured on 2026-08-28 against a live broker with the background queue
    unserved: a failed connector job was still RUNNING after 150 s, parked on `record_job`, having
    never reached `_notify_failure` — so the one message telling the chemist their job died sat
    behind an unbounded wait. `durable/notify.py` documents and measures the identical defect on
    the very next step, which is why the fix is its `schedule_to_close` doubling rather than a new
    knob.

    The two budgets are shortened so the test measures the *bound* rather than waiting out the
    shipped one; with neither, the run does not end at all.
    """
    from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow

    monkeypatch.setattr(settings, "background_task_queue", "nobody-serves-this-either")
    monkeypatch.setattr(settings, "job_record_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "activity_timeout_seconds", 2.0)

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
    """A `Worker` prepends the client's interceptors, so adding ours twice traced everything twice.

    Measured on 2026-08-28: `['TracingInterceptor', 'ChemclawWorkerInterceptor',
    'TracingInterceptor']`. The old test asserted only what `worker_interceptors()` *returns*,
    which is the half that was never wrong.
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
    raise AssertionError("the probe workflow never left RUNNING")


def test_a_failure_record_never_erases_a_finished_run_s_result() -> None:
    """The durable copy of a finished run survives the bookkeeping of the step that failed after it.

    Measured on 2026-08-28 against a live database, writing a failure record over the completed row
    for one job id:
    `{'summary': 'dG = -12.3 kJ/mol', 'result': {...}, 'note_id': 'note-1',
    'calc_refs': ['k1', 'k2'], 'state': 'completed'}` became
    `{'summary': '', 'result': {}, 'note_id': '', 'calc_refs': [], 'state': 'failed'}`. The upsert
    refreshed every mutable column from `EXCLUDED`, and `failed_job_record` supplies none of the
    five that say what a run produced — so the science of a finished run was destroyed by the
    record of a step that failed afterwards.

    Reachable independently of the workflow's own guard: `record_job`'s docstring names the case
    where the upsert commits and the activity then overruns its timeout, which leaves a row behind
    while the workflow believes there is none.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_run_that_fails_after_recording_is_not_recorded_a_second_time() -> None:
    """One run, one `chemclaw_jobs_finished_total` and one duration sample — either way it ended.

    `_finish` writes the completed record and *then* awaits three best-effort steps, which swallow
    `ActivityError` and nothing else. Anything else out of them — a `CancelledError` (a cancelled
    workflow was confirmed live to run its cleanup after one), a `ValidationError` — reached the
    `except BaseException` clause, which wrote a second record under the same job id: measured,
    `outcome="completed"` *and* `outcome="failed"` both at 1 for one run, and 2 observations on
    `chemclaw_job_duration_seconds`.

    Driven unsandboxed so the best-effort step can be made to raise the way a real one does; what
    is under test is the wrapper's own bookkeeping, not the sandbox.
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
    """A graceful drain is not a retry storm, and the alert that reads this series says it is.

    `chemclaw_activity_failures_total` was incremented for every `BaseException`, which the clause
    catches so the ambient context is unwound however an activity ends —
    `asyncio.CancelledError` included. A drain of eight activities therefore booked eight activity
    failures and `cancel_durable_job` booked one, while
    `deploy/helm/chemclaw/templates/prometheusrule.yaml`'s `ChemclawActivityRetryStorm` reads the
    series and asserts "Every attempt of this activity is failing".
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
    """The `finally` covers the statements that bound what it unbinds — the half that is visible.

    The three `set_current_*` calls, the in-flight increment and the start line used to run
    *above* the `try`, while the `finally`'s own comment claimed the contextvars were unbound
    "unconditionally". A `log_event` that raised therefore leaked all three tokens and one
    increment permanently.

    **What this test can see is the increment, and it used to claim the tokens too.** It asserted
    `get_current_actor() is None` after `asyncio.run(...)` returned, which is a different context:
    measured on this tree, a contextvar set inside `ActivityEnvironment.run` does not escape it at
    all (`(None, None)` for a probe reading before and after), so those three lines were true by
    construction and would have passed against the leaking code. That is the shape
    `D-2026-08-28-a-gate-that-cannot-fire-and-a-rate-with-no-denominator` deleted one file over in
    `tests/test_calc_jobs.py`, and this repository's rule for a check that cannot fail is that it
    does not stay. They are gone rather than rewritten because the harness isolates by design;
    `activities_in_flight()` is module state, survives the run, and is what actually goes red —
    measured against the pre-fix ordering: `assert 1 == 0`.
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
    """`str(cause)` has no length, and four stores downstream of it have no cap either.

    The string is written to a TEXT column, carried in the `job_failed` push-back payload, hashed
    into that event's dedupe key through `json.dumps`, stored in `session_events`, and read back
    into a `DurableJobStatus.summary` that lands in a model turn. `publish_results.py` already caps
    the analogous field at 500.
    """
    reason = failure_reason(ValueError("x" * 5000))
    assert len(reason) == 500
    assert reason == "x" * 500


def test_a_transport_fault_is_not_a_job_s_failure_reason() -> None:
    """A broker rolling during a poll used to become the run's own explanation.

    `failed_job_reason` carried an `except Exception` beneath its `WorkflowFailureError` clause, on
    the belief that a cancelled, terminated or timed-out run "reaches the client as its own
    exception type". `temporalio.client._workflow` raises `WorkflowFailureError` for all four bad
    endings, differing only in the `cause` the clause above already walks — so the broad clause
    caught nothing it was written for and one thing it was not, and `GET /jobs/{id}` answered
    `status="failed", summary="<gRPC UNAVAILABLE …>"` about a healthy run.
    """

    class _Handle:
        async def result(self) -> None:
            raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(RPCError):
        asyncio.run(failed_job_reason(_Handle()))


def test_the_job_duration_histogram_brackets_the_job_ceiling() -> None:
    """A p95 that saturates at 900 s cannot describe a job budgeted at 18,000.

    `chemclaw_job_duration_seconds` was bound to `_TOOL_BUCKETS`, whose top finite boundary is 900,
    while `connector_job_timeout_seconds` defaults to 18,000 and `xtb_job_timeout_seconds` to
    15,000. `histogram_quantile` returns the highest finite boundary rather than interpolating into
    `+Inf`, so the quantile pinned at exactly 900 s as jobs got expensive — verbatim the defect
    `_TURN_BUCKETS` was split off to fix one tier up.
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
    assert 'chemclaw_job_duration_seconds_bucket{connector="calc",le="18000"} 1' in rendered
