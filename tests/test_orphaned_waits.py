"""A durable wait whose run ended without settling it is settled by the sweep, and nothing else is.

Against a real broker and a real `pending_requests` table, because the whole decision is the
broker's answer: `durable/orphaned_waits.py` settles a row only when Temporal says the run that owns
it is not running, and a sweep that settled on anything weaker would cancel live questions
(`D-2026-09-25-a-wait-nobody-can-settle-is-settled-by-a-sweep`).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import orphaned_waits, pending_store
from chemclaw.durable.orphaned_waits import (
    OrphanedWaitsWorkflow,
    OrphanSweep,
    settle_orphaned_waits,
)
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_env_or_skip, start_local_env_or_skip

_QUEUE = "orphaned-waits-test"
_PREFIX = "orphan-test-"


@workflow.defn(name="OrphanTestHold")
class _Hold:
    """A run that stays open until it is terminated — the shape of a wait nobody has answered."""

    @workflow.run
    async def run(self) -> None:
        """Wait for a condition that never comes."""
        await workflow.wait_condition(lambda: False)


async def _open(request_id: str, run_id: str, *, age: str = "2 hours") -> None:
    """A waiting row owned by `run_id`, backdated past the sweep's grace window by `age`."""
    await pending_store.open_request(
        request_id=request_id,
        kind="measurement",
        subject=f"subject for {request_id}",
        rationale="",
        asked_of="",
        requested_by="u-1",
        session_id="",
        correlation_id="",
        due_at=datetime.now(UTC) + timedelta(days=7),
        run_id=run_id,
    )
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE pending_requests SET created_at = now() - %s::interval WHERE request_id = %s",
            (age, request_id),
        )
        await conn.commit()


async def _state(request_id: str) -> str:
    row = await pending_store.get_request(request_id)
    assert row is not None
    return row.state


def test_a_terminated_wait_is_settled_and_a_live_one_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminated, gone, running, reopened, and reset — five answers.

    - **terminated**: the case a `ParentClosePolicy.TERMINATE` parent leaves, which never reaches
      workflow code — settled `cancelled`, with the reason;
    - **unknown to the broker**: a history retention already removed — settled;
    - **running**: a question somebody may still answer — untouched, whatever its age;
    - **reopened**: the row now names a *live* run, while the sweep's evidence is about a dead
      one — untouched, because the settle is guarded on the run it examined;
    - **reset**: the row names a terminated run while a newer run of the same id carries the wait
      on — untouched, because whichever run of the id is running owns the question.
    """
    monkeypatch.setattr(settings, "awaiting_orphan_grace_seconds", 3600.0)

    async def _run() -> dict[str, str]:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "DELETE FROM pending_requests WHERE request_id LIKE %s", (f"{_PREFIX}%",)
            )
            await conn.commit()
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            monkeypatch.setattr(orphaned_waits, "connect", _returning(client))
            async with Worker(
                client,
                task_queue=_QUEUE,
                workflows=[_Hold],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                ids = {name: f"{_PREFIX}{name}" for name in ("terminated", "live", "reopened")}
                handles = {
                    name: await client.start_workflow(_Hold.run, id=wid, task_queue=_QUEUE)
                    for name, wid in ids.items()
                }
                await _open(ids["terminated"], handles["terminated"].first_execution_run_id or "")
                await _open(ids["live"], handles["live"].first_execution_run_id or "")
                await _open(f"{_PREFIX}gone", "5b7c1c0e-0000-4000-8000-000000000000")
                # The reopened row names the live run; the sweep will describe that run and find
                # it running — and the guard is exercised directly below against a stale run id.
                await _open(ids["reopened"], handles["reopened"].first_execution_run_id or "")
                await handles["terminated"].terminate(reason="a parent closed with TERMINATE")
                # A reset, as an operator's remedy leaves it: the row names a run that was
                # terminated, while a newer run under the same id carries the wait on.
                reset_id = f"{_PREFIX}reset"
                first = await client.start_workflow(_Hold.run, id=reset_id, task_queue=_QUEUE)
                await _open(reset_id, first.first_execution_run_id or "")
                await first.terminate(reason="reset")
                successor = await client.start_workflow(_Hold.run, id=reset_id, task_queue=_QUEUE)

                sweep = await settle_orphaned_waits()

                stale = await pending_store.settle_orphan(
                    ids["reopened"], "an-older-run", "a sweep's evidence about a previous run"
                )
                states = {
                    "reset": await _state(reset_id),
                    "terminated": await _state(ids["terminated"]),
                    "gone": await _state(f"{_PREFIX}gone"),
                    "live": await _state(ids["live"]),
                    "reopened": await _state(ids["reopened"]),
                    "stale_settle": str(stale),
                    "settled": ",".join(sorted(sweep.settled)),
                }
                for name in ("live", "reopened"):
                    await handles[name].terminate(reason="test over")
                await successor.terminate(reason="test over")
                row = await pending_store.get_request(ids["terminated"])
                states["reason"] = str((row.answer if row else {}).get("reason", ""))
                return states

    states = asyncio.run(_run())
    assert states["terminated"] == "cancelled", states
    assert states["gone"] == "cancelled", states
    assert states["live"] == "waiting", "a question somebody can still answer was cancelled"
    assert states["reopened"] == "waiting", states
    assert states["reset"] == "waiting", (
        "a reset wait was cancelled: its row names the terminated run, and the run carrying the "
        "question on is alive"
    )
    assert states["stale_settle"] == "False", "a sweep settled a run it never examined"
    assert states["settled"] == f"{_PREFIX}gone,{_PREFIX}terminated", states
    assert "terminated" in states["reason"], states["reason"]


def test_live_waits_older_than_an_orphan_do_not_starve_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More live waits than one page, all older than the orphan, and the orphan is still reached.

    Before the cursor every pass selected the same `awaiting_orphan_batch` oldest rows, found them
    running, and stopped — so an orphan newer than a page of healthy waits was never examined.
    """
    monkeypatch.setattr(settings, "awaiting_orphan_grace_seconds", 3600.0)
    monkeypatch.setattr(settings, "awaiting_orphan_batch", 2)

    async def _run() -> tuple[str, list[str], int]:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "DELETE FROM pending_requests WHERE request_id LIKE %s", (f"{_PREFIX}%",)
            )
            await conn.commit()
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            monkeypatch.setattr(orphaned_waits, "connect", _returning(client))
            async with Worker(
                client,
                task_queue=_QUEUE,
                workflows=[_Hold],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                live = []
                for index in range(3):
                    wid = f"{_PREFIX}starve-live-{index}"
                    handle = await client.start_workflow(_Hold.run, id=wid, task_queue=_QUEUE)
                    await _open(wid, handle.first_execution_run_id or "", age="3 hours")
                    live.append(handle)
                orphan = f"{_PREFIX}starve-orphan"
                await _open(orphan, "5b7c1c0e-0000-4000-8000-000000000001")

                sweep = await settle_orphaned_waits()

                for handle in live:
                    await handle.terminate(reason="test over")
                return await _state(orphan), sweep.settled, sweep.examined

    # Other rows in a shared table may sit in the same pages, so the counts are lower bounds.
    state, settled, examined = asyncio.run(_run())
    assert state == "cancelled", "an orphan behind a page of live waits was never examined"
    assert f"{_PREFIX}starve-orphan" in settled
    assert examined >= 4


def test_a_pass_that_spends_its_budget_resumes_where_it_stopped_rather_than_at_the_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass out of budget hands its position to the next, and the walk wraps at the end.

    Before the cursor survived a pass, every pass restarted at the oldest row, so an orphan behind
    more live waits than one pass could describe was never reached — the starvation the in-pass
    cursor fixed, moved to a longer table. A zero budget makes every pass exactly one page.
    """
    monkeypatch.setattr(settings, "awaiting_orphan_grace_seconds", 3600.0)
    monkeypatch.setattr(settings, "awaiting_orphan_batch", 2)
    monkeypatch.setattr(orphaned_waits, "_PASS_BUDGET_FRACTION", 0.0)

    async def _run() -> tuple[str, list[int], bool]:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "DELETE FROM pending_requests WHERE request_id LIKE %s", (f"{_PREFIX}%",)
            )
            await conn.commit()
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            monkeypatch.setattr(orphaned_waits, "connect", _returning(client))
            async with Worker(
                client,
                task_queue=_QUEUE,
                workflows=[_Hold],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                live = []
                for index in range(3):
                    wid = f"{_PREFIX}resume-live-{index}"
                    handle = await client.start_workflow(_Hold.run, id=wid, task_queue=_QUEUE)
                    await _open(wid, handle.first_execution_run_id or "", age="3 hours")
                    live.append(handle)
                orphan = f"{_PREFIX}resume-orphan"
                await _open(orphan, "5b7c1c0e-0000-4000-8000-000000000002")

                examined: list[int] = []
                sweep = await settle_orphaned_waits()
                examined.append(sweep.examined)
                # Bounded: other rows in a shared table may sit in the same pages.
                for _ in range(200):
                    if sweep.resume_after is None:
                        break
                    sweep = await settle_orphaned_waits(sweep.resume_after)
                    examined.append(sweep.examined)
                wrapped = sweep.resume_after is None

                for handle in live:
                    await handle.terminate(reason="test over")
                return await _state(orphan), examined, wrapped

    state, examined, wrapped = asyncio.run(_run())
    assert max(examined) <= 2, f"a zero-budget pass described more than one page: {examined}"
    assert len(examined) >= 2, "one pass reached everything, so nothing here resumed"
    assert state == "cancelled", "an orphan behind more live waits than one pass was never reached"
    assert wrapped, "the walk never reached the end of the table to wrap around"


def test_a_cursor_left_past_the_last_row_wraps_within_the_same_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass handed a cursor with nothing after it sweeps from the start rather than nothing.

    A pass that runs out of budget exactly on the table's last full page leaves `resume_after` on
    that page's last row. Without the in-pass wrap the next Schedule fire read an empty page and
    returned `examined=0`, so a whole interval swept nothing before the walk wrapped.
    """
    monkeypatch.setattr(settings, "awaiting_orphan_grace_seconds", 3600.0)

    async def _run() -> tuple[str, OrphanSweep]:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "DELETE FROM pending_requests WHERE request_id LIKE %s", (f"{_PREFIX}%",)
            )
            await conn.commit()
        async with await start_local_env_or_skip() as env:
            monkeypatch.setattr(orphaned_waits, "connect", _returning(pydantic_client(env)))
            orphan = f"{_PREFIX}wrap-orphan"
            await _open(orphan, "5b7c1c0e-0000-4000-8000-000000000003")
            # Past every eligible row: they are all older than the grace window, so older than now.
            past_the_end = pending_store.WaitingRow(request_id="~", created_at=datetime.now(UTC))
            sweep = await settle_orphaned_waits(past_the_end)
            return await _state(orphan), sweep

    state, sweep = asyncio.run(_run())
    assert sweep.examined >= 1, "a pass handed a cursor past the end examined nothing"
    assert state == "cancelled", "the orphan was not reached by the wrapped pass"
    assert f"{_PREFIX}wrap-orphan" in sweep.settled


_SEEN: list[pending_store.WaitingRow | None] = []


@activity.defn(name="settle_orphaned_waits")
async def _recording_sweep(after: pending_store.WaitingRow | None = None) -> OrphanSweep:
    """Stand-in for the sweep: record the cursor each run was handed, and hand back a new one."""
    _SEEN.append(after)
    return OrphanSweep(
        examined=2,
        resume_after=pending_store.WaitingRow(
            request_id=f"cursor-{len(_SEEN)}",
            run_id="run-a",
            created_at=datetime(2026, 9, 1, 12, 0, len(_SEEN), tzinfo=UTC),
        ),
    )


def test_each_scheduled_run_starts_from_the_previous_run_s_resume_after() -> None:
    """The workflow half of the cursor: a run reads the last completion result and passes it on.

    Every other test here calls the activity directly, so a `run` that dropped the argument — or a
    `WaitingRow` whose `datetime` did not survive the data converter — would pass all of them. A
    cron workflow is what gives a run a last completion result, and the sandboxed runner is what
    the real worker uses.
    """
    _SEEN.clear()

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            queue = "orphaned-waits-cron-test"
            async with Worker(
                client,
                task_queue=queue,
                workflows=[OrphanedWaitsWorkflow],
                activities=[_recording_sweep],
            ):
                handle = await client.start_workflow(
                    OrphanedWaitsWorkflow.run,
                    id="orphaned-waits-cron-test",
                    task_queue=queue,
                    cron_schedule="* * * * *",
                )
                for _ in range(10):
                    if len(_SEEN) >= 3:
                        break
                    await env.sleep(timedelta(minutes=1))
                await handle.terminate(reason="test over")

    asyncio.run(_run())
    assert len(_SEEN) >= 3, f"the cron workflow ran {len(_SEEN)} times"
    assert _SEEN[0] is None, "the first run had no previous result and should start at the oldest"
    for index, after in enumerate(_SEEN[1:3], start=1):
        assert after == pending_store.WaitingRow(
            request_id=f"cursor-{index}",
            run_id="run-a",
            created_at=datetime(2026, 9, 1, 12, 0, index, tzinfo=UTC),
        ), f"run {index + 1} was not handed run {index}'s resume_after: {after!r}"


def _returning(client: Any) -> Any:
    """`connect` replaced by one that hands back the test environment's client."""

    async def _connect() -> Any:
        return client

    return _connect
