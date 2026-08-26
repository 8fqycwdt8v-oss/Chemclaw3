"""Cost attribution: who spent what, on tokens and on compute.

The readiness row read *"token metrics carry `profile` only, so 'what did team X cost' is
unanswerable, and compute spend is entirely unmetered — no counter for jobs launched or
node-hours"*. Half of that is a real gap and half is not, and the tests below separate them:

- **Real.** Nothing durable recorded what a turn cost, against whom. The `profile` label answers a
  deployment-wide question; the budget tracker meters per user in memory *to refuse a turn* and
  resets on restart. Per-actor spend over a quarter had no store at all.
- **Not.** `chemclaw_jobs_started_total` has existed since D-118 (`connectors/jobs.py`). What was
  missing on the compute path is not a count but a *magnitude*: a two-second xTB call and a six-hour
  DFT run incremented it identically.

The one design decision worth a test of its own is why this is a table rather than an `actor` label:
`core/metrics` refuses a counter past 64 label series on purpose, because the value is
attacker-influenced. That refusal is the reason for the table, so it is asserted here rather than
described.
"""

import asyncio
import logging

import pytest

from chemclaw.agent.turn_cost import (
    NullTurnCostSink,
    TurnCost,
    default_turn_cost_sink,
    record_turn_cost,
)
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS, Metrics


class _RecordingSink:
    """A sink that keeps what it was handed."""

    def __init__(self) -> None:
        self.costs: list[TurnCost] = []

    async def record(self, cost: TurnCost) -> None:
        self.costs.append(cost)


class _FailingSink:
    """A sink whose write always fails, as a database that is down does."""

    async def record(self, cost: TurnCost) -> None:
        raise RuntimeError("database is down")


async def _drain() -> None:
    """Let the fire-and-forget write task run to completion."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_the_metric_registry_refuses_an_unbounded_label_which_is_why_this_is_a_table() -> None:
    """The premise of the whole design, asserted rather than asserted-in-prose.

    A per-actor token counter is the obvious fix and is not available: the registry caps a counter
    at 64 label series and refuses past it (D-152), because a label value is attacker-influenced and
    minting tokens for many `oid`s is exactly the way around a per-principal limit. Any deployment
    with more than 64 users would silently lose series — which is worse than not having them.
    """
    registry = Metrics()
    for index in range(200):
        registry.increment("chemclaw_tokens_total", 1.0, {"profile": f"actor-{index}"})
    series = [
        line for line in registry.render().splitlines() if line.startswith("chemclaw_tokens_total{")
    ]
    assert len(series) < 200, "the registry accepted unbounded label cardinality"


def test_a_turn_cost_carries_the_identity_the_metric_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap the table closes: spend booked against an actor, not only a profile."""
    sink = _RecordingSink()
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: sink)

    async def _run() -> None:
        record_turn_cost(
            TurnCost(
                correlation_id="cid-1",
                session_id="s-1",
                actor="oid-abc",
                profile="synthesis",
                input_tokens=100,
                output_tokens=20,
                duration_seconds=4.5,
            )
        )
        await _drain()

    asyncio.run(_run())
    assert [c.actor for c in sink.costs] == ["oid-abc"]
    assert sink.costs[0].input_tokens == 100


def test_recording_a_cost_never_awaits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner books this from a `finally` in which an `await` re-raises a pending cancellation.

    That block runs on the disconnect path too (D-130), and an `await` there would skip the five
    context-var resets after it, leaking one turn's ambient identity into the next turn on this
    worker. So the contract is that `record_turn_cost` is an ordinary function — and the way to
    prove it is to call it from a *cancelled* task and watch the write still land.
    """
    sink = _RecordingSink()
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: sink)

    async def _run() -> None:
        async def _turn() -> None:
            try:
                await asyncio.Event().wait()  # never completes; cancelled from outside
            finally:
                record_turn_cost(TurnCost(correlation_id="cid-cancelled", actor="oid-x"))

        task = asyncio.create_task(_turn())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _drain()

    asyncio.run(_run())
    assert [c.correlation_id for c in sink.costs] == ["cid-cancelled"], (
        "a turn torn down by a disconnect was not billed — the runaway case the ledger exists for"
    )


def test_a_failed_write_is_logged_and_never_escapes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Telemetry booked off the hot path must not escalate into the turn's teardown.

    An exception raised in the write task would surface only as an unattributed `Task exception was
    never retrieved` — the same trap the durable rollback documents — so the task swallows and logs.
    """
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _FailingSink)

    async def _run() -> None:
        with caplog.at_level(logging.WARNING):
            record_turn_cost(TurnCost(correlation_id="cid-doomed"))
            await _drain()

    asyncio.run(_run())
    assert "cid-doomed" in caplog.text


def test_no_database_means_no_write_task_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory-store deployment must not schedule a task per turn to drop the result.

    The same `session_store == "postgres"` switch the audit sink and the job record read: it is the
    deployment's statement that a database exists. Off it, the ledger is inert rather than busy.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    assert isinstance(default_turn_cost_sink(), NullTurnCostSink)

    scheduled: list[str] = []

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        original = loop.create_task

        def _watch(coro, **kwargs):  # type: ignore[no-untyped-def]
            # By qualname, because the loop schedules its own shutdown coroutines during
            # `asyncio.run` teardown and counting those would make this assertion always fail.
            scheduled.append(getattr(coro, "__qualname__", ""))
            return original(coro, **kwargs)

        monkeypatch.setattr(loop, "create_task", _watch)
        record_turn_cost(TurnCost(correlation_id="cid-nowhere"))
        await _drain()

    asyncio.run(_run())
    writes = [name for name in scheduled if "record_turn_cost" in name]
    assert not writes, f"a null sink still scheduled a write task: {writes}"


# --- the compute half -------------------------------------------------------------------------


def test_a_job_record_carries_what_the_run_consumed() -> None:
    """`job_records` said what ran and why, and nothing about how much of the cluster it took.

    So the durable record of the most expensive thing this system does could not distinguish a
    two-second xTB call from a six-hour DFT run, and `chemclaw_jobs_started_total` — which does
    exist, contrary to the row that asked for it — counted them identically.
    """
    from chemclaw.durable.connector_job import (
        ConnectorJobInput,
        ConnectorJobResult,
        job_record_for,
    )

    job = ConnectorJobInput(
        connector="calc",
        job="sample_conformers",
        workflow="CalcJobWorkflow",
        task_queue="connector-calc",
        rationale="check the barrier",
        requested_by="oid-abc",
        payload={"smiles": "CCO"},
    )
    record = job_record_for(
        "wf-1", job, ConnectorJobResult(summary="done"), runtime_seconds=21600.0
    )
    assert record.runtime_seconds == 21600.0


def test_finished_job_runtime_reaches_the_consumption_counter() -> None:
    """A launch counter is the least informative number available on the expensive path.

    Accumulated seconds is the consumption shape — `rate()` reads as compute-seconds per second,
    the same shape as the token counters — and it is labelled by connector, which is bounded by the
    chart exactly as `profile` is.
    """
    registry = Metrics()
    registry.increment("chemclaw_job_runtime_seconds_total", 21600.0, {"connector": "qm"})
    registry.increment("chemclaw_job_runtime_seconds_total", 2.0, {"connector": "calc"})
    rendered = registry.render()
    assert 'chemclaw_job_runtime_seconds_total{connector="qm"} 21600' in rendered
    assert 'chemclaw_job_runtime_seconds_total{connector="calc"} 2' in rendered


def test_the_wrapper_measures_the_run_rather_than_hardcoding_it() -> None:
    """The one claim on this path that no offline test could otherwise hold.

    `job_record_for` is pure and testable, but it takes `runtime_seconds` as an argument — so it
    passes just as happily on a hardcoded `0.0` as on a measurement, and the place the measurement
    actually happens is `ConnectorJobWorkflow.run`, which needs a Temporal server. The end-to-end
    test there cannot supply a lower bound either: the fixture child returns immediately, and the
    time-skipping server may legitimately report both of the wrapper's clock reads as the same
    instant, so `> 0` would be a flake rather than an assertion. (A sleep in the fixture to force a
    gap was tried and broke that test outright — the shared harness is the wrong place to buy this.)

    So the claim is checked where it is cheap and exact: over the AST. The argument must be a
    *computed expression* mentioning `workflow.now`, never a constant. Parsed rather than
    string-matched, because a substring check is satisfied by the comment above the line — a trap
    this repository has already fallen into twice (`tasks/lessons.md`).
    """
    import ast
    import inspect

    from chemclaw.durable import connector_job

    tree = ast.parse(inspect.getsource(connector_job))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "job_record_for"
    ]
    assert calls, "the wrapper no longer builds a job record"
    for call in calls:
        runtime = next((kw.value for kw in call.keywords if kw.arg == "runtime_seconds"), None)
        assert runtime is not None, "the wrapper builds a record without a runtime"
        assert not isinstance(runtime, ast.Constant), (
            "runtime_seconds is a literal — the record would report every run as costing the same"
        )
        assert "workflow.now" in ast.unparse(runtime), (
            "runtime_seconds is not measured from the workflow's own clock, so a replay would "
            "measure the replay"
        )


def test_the_runtime_counter_is_declared_on_the_process_registry() -> None:
    """The activity books it through the metrics bridge, which needs the name to be declared."""
    assert "chemclaw_job_runtime_seconds_total" in METRICS.render()
