"""Cost attribution: who spent what, on tokens and on compute.

Turn costs are recorded durably per actor in a table, and job records carry the runtime a job
consumed, so magnitude rather than just a launch count is visible. It is a table rather than an
`actor` metric label because `core/metrics` refuses unbounded, attacker-influenced label series.
"""

import asyncio
import logging
from pathlib import Path

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
    """The metric registry refuses an unbounded label, which is why per-actor spend is a table.

    Driven one past `_MAX_SERIES_PER_COUNTER` rather than at a fixed count, so raising the cap
    cannot turn this into a test of nothing.
    """
    from chemclaw.core.metrics import _MAX_SERIES_PER_COUNTER

    registry = Metrics()
    for index in range(_MAX_SERIES_PER_COUNTER + 1):
        registry.increment("chemclaw_tokens_total", 1.0, {"profile": f"actor-{index}"})
    series = [
        line for line in registry.render().splitlines() if line.startswith("chemclaw_tokens_total{")
    ]
    assert len(series) == _MAX_SERIES_PER_COUNTER, (
        "the registry accepted unbounded label cardinality"
    )


async def test_a_turn_cost_carries_the_identity_the_metric_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap the table closes: spend booked against an actor, not only a profile."""
    sink = _RecordingSink()
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: sink)

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

    assert [c.actor for c in sink.costs] == ["oid-abc"]
    assert sink.costs[0].input_tokens == 100


async def test_recording_a_cost_never_awaits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording a cost never awaits.

    The runner books it from a `finally` where an `await` re-raises a pending cancellation and would
    skip the context-var resets after it. Proven by calling it from a cancelled task.
    """
    sink = _RecordingSink()
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: sink)

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

    assert [c.correlation_id for c in sink.costs] == ["cid-cancelled"], (
        "a turn torn down by a disconnect was not billed — the runaway case the ledger exists for"
    )


async def test_a_failed_write_is_logged_and_never_escapes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Telemetry booked off the hot path must not escalate into the turn's teardown.

    An exception raised in the write task would surface only as an unattributed `Task exception was
    never retrieved` — the same trap the durable rollback documents — so the task swallows and logs.
    """
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _FailingSink)

    with caplog.at_level(logging.WARNING):
        record_turn_cost(TurnCost(correlation_id="cid-doomed"))
        await _drain()

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
    """A job record carries the runtime the job consumed."""
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
    """Finished job runtime reaches the consumption counter.

    Accumulated seconds labelled by connector, so `rate()` reads as compute-seconds per second.
    """
    registry = Metrics()
    registry.increment("chemclaw_job_runtime_seconds_total", 21600.0, {"connector": "qm"})
    registry.increment("chemclaw_job_runtime_seconds_total", 2.0, {"connector": "calc"})
    rendered = registry.render()
    assert 'chemclaw_job_runtime_seconds_total{connector="qm"} 21600' in rendered
    assert 'chemclaw_job_runtime_seconds_total{connector="calc"} 2' in rendered


def test_the_wrapper_measures_the_run_rather_than_hardcoding_it() -> None:
    """The wrapper passes a measured runtime, not a constant.

    `ConnectorJobWorkflow.run` needs a Temporal server, and a time-skipping server can report both
    clock reads as one instant, so this is checked over the AST: the argument must be a computed
    expression mentioning `workflow.now`. Parsed rather than string-matched, so a comment cannot
    satisfy it.
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


def test_every_turn_cost_reader_has_the_surface_that_asks_it() -> None:
    """Each reader of the ledger ships with the route, command or report that asks it.

    A query function with no caller claims a capability that does not exist. The list is exhaustive,
    so a reader added without a surface fails here:

    - `operations/activity.py`: the aggregate read model behind the `review_activity` tool.
    - `operations/evidence_pack.py`: `assemble`, the context-of-use record for one session.
    - `cli/distill.py`: `make distill`, reading `skills_loaded` for the self-confirmation guard.
    - `cli/explain.py`: `python -m chemclaw.cli.explain`, the audit reconstruction.
    - `cli/live_turn_cost.py`: `make live-turn-cost`, reading back only the session it opened.
    - `evals/delegation_run.py`: `make live-delegation`, reading back the billed tokens of the
      sessions
      it drove.
    - `agent/session_store.py`: `PostgresHistoryProvider.mark_interrupted`, which checks whether an
      interrupted turn already has a row so its outcome is never booked twice.

    Tests read the table with their own SQL.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    readers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "FROM turn_costs" in path.read_text(encoding="utf-8")
    )
    assert readers == [
        "agent/session_store.py",
        "cli/distill.py",
        "cli/explain.py",
        "cli/live_turn_cost.py",
        "evals/delegation_run.py",
        "operations/activity.py",
        "operations/evidence_pack.py",
    ], (
        f"{readers} reads `turn_costs`. Each reader must ship with the route, command or report "
        "that asks it, and be listed here with it. A reader with no surface is the 2026-08-27 "
        "defect."
    )
    import chemclaw.agent.operations_tools  # noqa: F401  (registers the tool)
    from chemclaw.core.tool_registry import registered_tool_names

    assert "review_activity" in registered_tool_names(), (
        "`operations.spend` reads the ledger and nothing advertises it — which is exactly the "
        "shape the two deleted readers had."
    )
