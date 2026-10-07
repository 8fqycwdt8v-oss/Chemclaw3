"""Child-workflow fan-out: the batching helper offline and the real thing on Temporal.

`fan_out` runs on the time-skipping test server (skips offline): N inputs → N children, results in
input order, and a child that raises is dropped while its siblings still return.
"""

import asyncio
import logging
import types
from typing import Any

import pytest
from temporalio import workflow

# This module defines a workflow, so Temporal's sandbox re-imports its imports during validation.
# `chemclaw.config` (`Path.expanduser()` via pydantic-settings) and the test-harness imports
# (`urllib.request`) would fail it; production workflow modules pass `chemclaw.config` through the
# same way.
with workflow.unsafe.imports_passed_through():
    from temporalio.client import Client
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.orchestrator import _batches, fan_out
    from tests.temporal_env import pydantic_client, start_env_or_skip


def test_batches_splits_in_order() -> None:
    """Inputs are chunked into consecutive batches of at most `size`, order preserved."""
    assert _batches([0, 1, 2, 3, 4], 2) == [[0, 1], [2, 3], [4]]
    assert _batches([], 3) == []
    assert _batches([1], 3) == [[1]]


@workflow.defn(failure_exception_types=[Exception])
class _DoublerWorkflow:
    """A trivial child: doubles its input, or raises on the poison value 13.

    `failure_exception_types=[Exception]` is required (D-093): otherwise the SDK treats a plain
    exception as a workflow bug and retries the task forever, ignoring `RetryPolicy`, instead of
    failing the execution as `fan_out`'s isolation contract expects.
    """

    @workflow.run
    async def run(self, value: int) -> int:
        if value == 13:
            raise ValueError("poison input")
        return value * 2


@workflow.defn
class _FanOutParent:
    """A parent that fans its inputs out to `_DoublerWorkflow` children via `fan_out`."""

    @workflow.run
    async def run(self, values: list[int]) -> list[int]:
        return await fan_out(_DoublerWorkflow, values, id_prefix="dbl", max_parallel=2)


async def test_fan_out_runs_children_in_order_and_isolates_failures() -> None:
    """Each input runs as a child; a poison child is dropped, the rest return in input order."""
    async with await start_env_or_skip() as env:
        client: Client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[_FanOutParent, _DoublerWorkflow],
        ):
            out = await client.execute_workflow(
                _FanOutParent.run,
                [1, 2, 13, 4, 5],
                id="fan-out-test",
                task_queue=settings.background_task_queue,
            )
    assert out == [2, 4, 8, 10]  # 13 dropped (poison), others doubled, input order kept


def test_fan_out_limit_is_resolved_via_an_activity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The default concurrency bound comes from a recorded activity, not a live settings read.

    A settings read in workflow code would change the batch size on replay after a config change, a
    nondeterminism error; the activity records the value in history.
    """
    from temporalio.testing import ActivityEnvironment

    from chemclaw.core.config import settings
    from chemclaw.durable.orchestrator import resolve_fan_out_limit

    monkeypatch.setattr(settings, "orchestrator_max_parallel_children", 3)
    assert asyncio.run(ActivityEnvironment().run(resolve_fan_out_limit)) == 3


def test_background_worker_registers_fan_out_limit_activity() -> None:
    """Every worker hosting a fan-out parent must serve the limit-resolving activity."""
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES
    from chemclaw.durable.orchestrator import resolve_fan_out_limit

    assert resolve_fan_out_limit in BACKGROUND_ACTIVITIES


def _fake_workflow(*, replaying: bool = False) -> types.SimpleNamespace:
    """A stand-in for `temporalio.workflow` inside `orchestrator`.

    The dropped-child counting reads only `workflow.info()`, `workflow.logger` and
    `workflow.unsafe.is_replaying()`, so substituting those (and `_run_child`) tests it offline, as
    `test_publish.py::_fake_workflow` does.
    """
    return types.SimpleNamespace(
        logger=logging.getLogger("test.workflow"),
        unsafe=types.SimpleNamespace(is_replaying=lambda: replaying),
        info=lambda: types.SimpleNamespace(workflow_id="fan-out-parent"),
    )


async def _failing_run_child(*_args: Any, **_kwargs: Any) -> Any:
    raise ValueError("boom")


def test_a_replayed_dropped_child_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped child is not counted again on replay.

    Replay re-executes workflow code, so `chemclaw_fan_out_children_dropped_total` is guarded by
    `is_replaying`, like its siblings in `publish.py`.
    """
    import chemclaw.durable.orchestrator as orchestrator_module
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(orchestrator_module, "workflow", _fake_workflow(replaying=True))
    monkeypatch.setattr(orchestrator_module, "_run_child", _failing_run_child)
    before = METRICS.value("chemclaw_fan_out_children_dropped_total")

    result = asyncio.run(orchestrator_module.fan_out(object(), [1], id_prefix="t", max_parallel=1))

    assert result == []
    assert METRICS.value("chemclaw_fan_out_children_dropped_total") == before


def test_a_dropped_child_is_counted_when_not_replaying(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is on the replay path only — a real drop during normal execution still counts."""
    import chemclaw.durable.orchestrator as orchestrator_module
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(orchestrator_module, "workflow", _fake_workflow(replaying=False))
    monkeypatch.setattr(orchestrator_module, "_run_child", _failing_run_child)
    before = METRICS.value("chemclaw_fan_out_children_dropped_total")

    result = asyncio.run(orchestrator_module.fan_out(object(), [1], id_prefix="t", max_parallel=1))

    assert result == []
    assert METRICS.value("chemclaw_fan_out_children_dropped_total") == before + 1


@workflow.defn
class _UndeclaredChildWorkflow:
    """A child that declared no `failure_exception_types` — the state the guard below refuses.

    Module level rather than local, because `@workflow.run` refuses a local class outright: the
    thing under test is a real workflow definition, not a stub.
    """

    @workflow.run
    async def run(self, value: int) -> int:
        return value


def test_fan_out_refuses_a_child_that_declared_no_way_to_fail() -> None:
    """`fan_out` refuses a child that declared no way to fail.

    Without `failure_exception_types` a raising child parks until `execution_timeout` and is logged
    exactly like a hang. `tests/test_workflow_registry.py` covers the job-path registry; this checks
    the child actually passed to `fan_out`.
    """
    with pytest.raises(ValueError, match="failure_exception_types"):
        asyncio.run(fan_out(_UndeclaredChildWorkflow, [1], id_prefix="t", max_parallel=1))


def test_fan_out_accepts_the_children_it_actually_ships_with() -> None:
    """The guard accepts both shipped `fan_out` children and the unit-test double.

    A `child` without `__temporal_workflow_definition` is a stand-in, not a workflow whose failure
    mode is in question.
    """
    from chemclaw.durable.memory_jobs import PublishNoteWorkflow
    from chemclaw.durable.orchestrator import _refuse_a_child_that_cannot_fail
    from chemclaw.durable.report_workflow import ReportSectionWorkflow

    for child in (PublishNoteWorkflow, ReportSectionWorkflow, _DoublerWorkflow, object()):
        _refuse_a_child_that_cannot_fail(child)
