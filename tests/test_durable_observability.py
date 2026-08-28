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
import logging
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ExecuteActivityInput,
)

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.logging import ContextFilter
from chemclaw.core.metrics import Metrics
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.temporal_client import connect_options
from chemclaw.durable.connector_job import ConnectorJobInput, failed_job_record
from chemclaw.durable.interceptor import (
    ChemclawWorkerInterceptor,
    activities_in_flight,
    activity_context,
    draining,
)
from chemclaw.durable.job_metrics import bind_job_gauges, job_ended, job_running, jobs_in_flight
from chemclaw.durable.job_record import JobRecord, record_job
from chemclaw.durable.job_record_store import PostgresJobRecordSink, read_job_record
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
    # Roles are NOT lifted from the payload — a relayed workflow argument is not a verified role
    # claim, so binding it would let anyone who can enqueue the activity forge a privileged role
    # (security review). The actor crosses for attribution; roles bind empty (fail-closed).
    assert context.roles == frozenset()


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


def test_the_in_flight_gauge_reads_the_jobs_this_process_carries() -> None:
    """The in-flight gauge reads the durable jobs this process is carrying.

    A set of workflow ids, so a *replayed* job this worker already carries adds nothing —
    which is what lets the workflow body touch it with no `is_replaying` guard.
    """
    metrics = Metrics()
    with _using(metrics):
        bind_job_gauges()
        job_running("job-1")
        job_running("job-1")
        job_running("job-2")
        assert jobs_in_flight() == 2.0
        assert "chemclaw_jobs_in_flight 2" in metrics.render()
        job_ended("job-1")
        job_ended("job-2")
        assert "chemclaw_jobs_in_flight 0" in metrics.render()


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
    monkeypatch.setattr(settings, "temporal_metrics_port", 39100)
    monkeypatch.setattr(settings, "temporal_metrics_host", "127.0.0.1")
    # One per process: a `Runtime` owns a Rust core and a bound socket, so a second is either a
    # bind failure or an exposition nobody scrapes. `monkeypatch` restores `_RUNTIME` to `None`
    # at teardown, so the bound port does not outlive this test.
    assert "runtime" in connect_options()
    assert connect_options()["runtime"] is connect_options()["runtime"]


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
