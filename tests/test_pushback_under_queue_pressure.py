"""A finished job's push-back and its durable record must survive a busy queue, not just a free one.

`background-jobs` is one worker with eight activity slots, and it carries everything: the connector
job wrapper, 900 s template agent steps, 300 s report sections, the hourly sweeps — and, at the end
of every job, two small writes that decide whether anybody ever learns the job finished. Those two
carried a `schedule_to_close_timeout` of twice their own work budget (60 s at the shipped defaults),
which is a *total* and therefore mostly a bound on a wait neither call can control.

Measured on the real broker while writing this: with the slots held by long activities a 50 ms
activity waited 41.6 s, and the shipped shape (30 s start-to-close, 60 s schedule-to-close, five
attempts) behind eight 120 s activities was dropped at 60.1 s with `ActivityError: Activity task
timed out`. At 200 users the expected wait for a slot is ~150 s, so essentially every completion
push-back was dropped — the chemist's session showing "running" forever — and essentially every
`job_records` row lost, the row whose own comment says it holds what Temporal's history will not.
Both failures are swallowed by design, so the only trace was a counter.

**The fix is to bound the two quantities separately**, which is what Temporal has these timeouts
for: `start_to_close_timeout` bounds the *work* (one small insert) and `schedule_to_start_timeout`
bounds the *wait*. This file drives that on a real-time server, because the defect is about
wall-clock contention for a worker slot and the time-skipping server fast-forwards exactly the
contention under test.

The queue is narrowed to one slot rather than eight so the shape is reproduced in seconds; nothing
else about it is scaled, and the assertion is written against the bound the code used to carry
rather than against a wall-clock constant, so it stays a statement about the change.
"""

import asyncio
import time
from datetime import timedelta

import pytest
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.client import Client
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable import notify as notify_module
    from chemclaw.durable.notify import notify_session_best_effort, record_session_event_activity
    from chemclaw.durable.publish import light_write_queue_wait_timeout
    from tests.temporal_env import pydantic_client, start_local_env_or_skip

# How long the single slot is held, and how long the test is prepared to wait for the push-back
# behind it. The hold is comfortably longer than the bound the two calls used to carry
# (`activity_timeout_seconds * 2`, set to 1.0 s below), which is what makes the assertion a
# counterfactual rather than a timing coincidence.
_HOLD_SECONDS = 6.0


@activity.defn(name="hold-one-slot")
async def hold_one_slot(seconds: float) -> None:
    """Occupy an activity slot for `seconds`, the way a template step or a sync sweep does."""
    await asyncio.sleep(seconds)


@workflow.defn
class HoldWorkflow:
    """Take the worker's only activity slot, so the push-back behind it has to queue."""

    @workflow.run
    async def run(self, seconds: float) -> None:
        """Run the holding activity, bounded so a hung test fails rather than hangs."""
        await workflow.execute_activity(
            hold_one_slot,
            seconds,
            start_to_close_timeout=timedelta(seconds=seconds + 30),
            schedule_to_start_timeout=light_write_queue_wait_timeout(),
        )


@workflow.defn
class PushBackWorkflow:
    """A completing job's push-back, called exactly as every real caller calls it."""

    @workflow.run
    async def run(self, session_id: str) -> bool:
        """Return whether the session was told — the value every caller is free to ignore."""
        return await notify_session_best_effort(session_id, "job_finished", {"job": "j-1"})


def test_a_push_back_behind_a_busy_queue_is_delivered_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion reaches the session after a wait that the old bound would have failed.

    Three settings are narrowed so this runs in seconds: the work budget, the queue bound
    (`template_step_timeout_seconds`, which is what `light_write_queue_wait_timeout` reads), and
    the slot count. The proof is the pair of assertions at the end — the push-back succeeded, *and*
    it took longer than `activity_timeout_seconds * 2`, which is precisely the total budget the
    call used to carry. Under the shipped code before this change that is the point at which
    Temporal returned `Activity task timed out` and `notify_session_best_effort` swallowed it.
    """
    monkeypatch.setattr(settings, "activity_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "template_step_timeout_seconds", 60.0)

    recorded: list[tuple[str, str]] = []

    async def _record(session_id: str, kind: str, payload: object, **kwargs: object) -> None:
        recorded.append((session_id, kind))

    # The insert itself is not what this test is about, and driving it against Postgres would make
    # a queue-contention test depend on a database being up. The activity body is otherwise the
    # real one: the same registration, the same queue, the same timeouts.
    monkeypatch.setattr(notify_module, "record_session_event", _record)

    async def _run() -> tuple[bool, float]:
        async with await start_local_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[HoldWorkflow, PushBackWorkflow],
                activities=[hold_one_slot, record_session_event_activity],
                max_concurrent_activities=1,
            ):
                holding = await client.start_workflow(
                    HoldWorkflow.run,
                    _HOLD_SECONDS,
                    id="holds-the-only-slot",
                    task_queue=settings.background_task_queue,
                )
                # Let the holder actually claim the slot; without this the push-back can win the
                # race and the test proves nothing.
                await asyncio.sleep(1.0)
                started = time.perf_counter()
                delivered = await client.execute_workflow(
                    PushBackWorkflow.run,
                    "session-under-pressure",
                    id="push-back-behind-a-busy-queue",
                    task_queue=settings.background_task_queue,
                )
                waited = time.perf_counter() - started
                await holding.result()
                return delivered, waited

    delivered, waited = asyncio.run(_run())

    assert delivered is True
    assert recorded == [("session-under-pressure", "job_finished")]
    old_total_budget = settings.activity_timeout_seconds * 2
    assert waited > old_total_budget, (
        f"the push-back was delivered in {waited:.1f}s, inside the {old_total_budget:.1f}s total "
        "budget it used to carry — the queue was not actually contended, so this run is not "
        "evidence about the defect"
    )
