"""Child-workflow fan-out (plan F10-D1): the batching helper offline + the real thing on Temporal.

`_batches` is pure and tested directly. `fan_out` itself needs a Temporal server, so a small child
workflow proves the end-to-end contract on the time-skipping test server (skips offline, runs in
CI): N inputs → N children, results in input order, and a child that raises is isolated and dropped
while its siblings still return.
"""

import asyncio
import logging
import types
from typing import Any

import pytest
from temporalio import workflow

# This module *defines* a workflow (`_FanOutParent`), so Temporal's sandbox re-imports it — and
# everything it imports — inside the sandbox when validating that workflow. Two of those imports
# execute code the sandbox forbids: `chemclaw.config` constructs `Settings()`, whose
# pydantic-settings `env_file` resolution calls `Path.expanduser()`, and the test-harness/client
# imports reach `urllib.request`. Either one fails the whole worker with
# "Failed validating workflow _FanOutParent" — the CI-only failure this guard fixes (it skips
# offline, where no Temporal test server is available).
#
# Passing them through is the established pattern here, not a workaround: every production workflow
# module already wraps its `chemclaw.config` import exactly this way (`durable/orchestrator.py`,
# `retention.py`, `digest.py`, …). None of this is workflow code — it is settings plus the test
# harness — so none of it needs the sandbox's determinism checks.
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

    `failure_exception_types=[Exception]` is required (D-093), not decoration: by default the
    Temporal SDK treats a raw (non-`FailureError`) exception raised in workflow code as a possible
    *bug* and suspends the workflow via an internal task-failure retry loop that ignores any
    `RetryPolicy` entirely and never gives up — so the poison input's plain `ValueError` would hang
    the workflow forever instead of producing the `WorkflowExecutionFailed` the `fan_out` isolation
    contract (and `orchestrator.BAD_DATA_RETRY` default) actually depends on. This is exactly the
    CI-only hang `ci.yml`'s own comment described.
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


def test_fan_out_runs_children_in_order_and_isolates_failures() -> None:
    """Each input runs as a child; a poison child is dropped, the rest return in input order."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_fan_out_limit_is_resolved_via_an_activity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The default concurrency bound comes from a recorded activity, not a live settings read.

    Reading `orchestrator_max_parallel_children` inside workflow code would change how many
    StartChildWorkflow commands a replayed task emits when the config changes mid-flight — a
    nondeterminism error that wedges every in-flight fan-out parent. The activity records the
    value in history, so replay always sees the batch size the original execution used.
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
    """A stand-in for `temporalio.workflow` inside `orchestrator`, mirroring `test_publish.py`'s.

    `fan_out` itself needs a real Temporal server for the child-workflow round trip
    (`test_fan_out_runs_children_in_order_and_isolates_failures` above), but the dropped-child
    counting logic does not — it only reads `workflow.info()`, `workflow.logger` and
    `workflow.unsafe.is_replaying()`. Substituting the module's `workflow` reference (and
    `_run_child`, so no child workflow is actually started) tests that logic offline, the same way
    `test_publish.py::_fake_workflow` tests `publish_note_best_effort`'s replay guard.
    """
    return types.SimpleNamespace(
        logger=logging.getLogger("test.workflow"),
        unsafe=types.SimpleNamespace(is_replaying=lambda: replaying),
        info=lambda: types.SimpleNamespace(workflow_id="fan-out-parent"),
    )


async def _failing_run_child(*_args: Any, **_kwargs: Any) -> Any:
    raise ValueError("boom")


def test_a_replayed_dropped_child_is_not_counted_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay re-executes workflow code; counting a dropped child there would inflate the metric.

    The regression this pins: `chemclaw_fan_out_children_dropped_total` used to increment with no
    `is_replaying` guard, unlike its two siblings in `publish.py` — so a worker restart or
    sticky-cache eviction mid-fan-out counted every already-seen dropped child again.
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
    """A child that cannot fail does not fail — it *parks*, and the log calls that a timeout.

    Measured over three children (ok / raise / hang): the raising one and the hanging one produced
    the identical `fan-out child … failed and was dropped: Child Workflow execution timed out`,
    each after the full `fan_out_child_timeout_seconds`, because the SDK put the plain `ValueError`
    into the task-failure loop that ignores `retry_policy` and only `execution_timeout` freed it.
    An hour of somebody's time went on a distinction the log had erased.

    `tests/test_workflow_registry.py` already asserts the declaration over the *job path* registry,
    which is what covers a bundle added later; what neither it nor the six deliberate parkers it
    allows can see is a third `fan_out` caller whose child is on neither list. This checks the
    child that is actually passed, at the one seam that knows it is a fan-out child.
    """
    with pytest.raises(ValueError, match="failure_exception_types"):
        asyncio.run(fan_out(_UndeclaredChildWorkflow, [1], id_prefix="t", max_parallel=1))


def test_fan_out_accepts_the_children_it_actually_ships_with() -> None:
    """The guard is a declaration check, not a registry lookup, so it must not refuse a real child.

    Driven over both shipped `fan_out` children and over the double the two unit tests above pass:
    a `child` carrying no `__temporal_workflow_definition` is a stand-in for the SDK rather than a
    workflow whose failure mode is in question, and refusing it would break the seam this guard
    exists to protect.
    """
    from chemclaw.durable.memory_jobs import PublishNoteWorkflow
    from chemclaw.durable.orchestrator import _refuse_a_child_that_cannot_fail
    from chemclaw.durable.report_workflow import ReportSectionWorkflow

    for child in (PublishNoteWorkflow, ReportSectionWorkflow, _DoublerWorkflow, object()):
        _refuse_a_child_that_cannot_fail(child)
