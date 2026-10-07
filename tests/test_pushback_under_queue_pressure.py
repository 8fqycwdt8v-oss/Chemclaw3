"""A finished job's push-back and durable record survive a busy queue, not just a free one.

`background-jobs` runs everything on a few slots, so the two small completion writes (session
push-back and `job_records` row) wait for a slot behind long activities. A `schedule_to_close`
total would mostly bound that wait and drop both writes under load. They are bounded separately:
`start_to_close_timeout` for the work, `schedule_to_start_timeout` for the wait. Driven on a
real-time server (the time-skipping server would fast-forward the contention) with the queue
narrowed to one slot.
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

# How long the single slot is held, and how long the test waits for the push-back behind it. Longer
# than a combined `activity_timeout_seconds * 2` bound (1.0 s below), so the assertion is a
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
    """A push-back behind a busy queue is delivered rather than dropped.

    The work budget, the queue bound (`template_step_timeout_seconds`, read by
    `light_write_queue_wait_timeout`) and the slot count are narrowed. The push-back must succeed
    and take longer than `activity_timeout_seconds * 2`, the total a single schedule-to-close bound
    would allow.
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
