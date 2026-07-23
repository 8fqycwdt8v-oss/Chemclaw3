"""Child-workflow fan-out (plan F10-D1): the batching helper offline + the real thing on Temporal.

`_batches` is pure and tested directly. `fan_out` itself needs a Temporal server, so a small child
workflow proves the end-to-end contract on the time-skipping test server (skips offline, runs in
CI): N inputs → N children, results in input order, and a child that raises is isolated and dropped
while its siblings still return.
"""

import asyncio

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.config import settings
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.orchestrator import _batches, fan_out


def test_batches_splits_in_order() -> None:
    """Inputs are chunked into consecutive batches of at most `size`, order preserved."""
    assert _batches([0, 1, 2, 3, 4], 2) == [[0, 1], [2, 3], [4]]
    assert _batches([], 3) == []
    assert _batches([1], 3) == [[1]]


@workflow.defn
class _DoublerWorkflow:
    """A trivial child: doubles its input, or raises on the poison value 13."""

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
