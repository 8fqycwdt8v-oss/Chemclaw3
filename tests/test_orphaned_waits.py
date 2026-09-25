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
from temporalio import workflow
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable import orphaned_waits, pending_store
from chemclaw.durable.orphaned_waits import settle_orphaned_waits
from tests.pg import migrated_db_or_skip
from tests.temporal_env import pydantic_client, start_local_env_or_skip

_QUEUE = "orphaned-waits-test"
_PREFIX = "orphan-test-"


@workflow.defn(name="OrphanTestHold")
class _Hold:
    """A run that stays open until it is terminated — the shape of a wait nobody has answered."""

    @workflow.run
    async def run(self) -> None:
        """Wait for a condition that never comes."""
        await workflow.wait_condition(lambda: False)


async def _open(request_id: str, run_id: str) -> None:
    """A waiting row owned by `run_id`, backdated past the sweep's grace window."""
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
            "UPDATE pending_requests SET created_at = now() - interval '2 hours' "
            "WHERE request_id = %s",
            (request_id,),
        )
        await conn.commit()


async def _state(request_id: str) -> str:
    row = await pending_store.get_request(request_id)
    assert row is not None
    return row.state


def test_a_terminated_wait_is_settled_and_a_live_one_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminated, gone from the broker, running, and reopened under a new run — four answers.

    - **terminated**: the case a `ParentClosePolicy.TERMINATE` parent leaves, which never reaches
      workflow code — settled `cancelled`, with the reason;
    - **unknown to the broker**: a history retention already removed — settled;
    - **running**: a question somebody may still answer — untouched, whatever its age;
    - **reopened**: the row now names a *live* run, while the sweep's evidence is about a dead
      one — untouched, because the settle is guarded on the run it examined.
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

                sweep = await settle_orphaned_waits()

                stale = await pending_store.settle_orphan(
                    ids["reopened"], "an-older-run", "a sweep's evidence about a previous run"
                )
                states = {
                    "terminated": await _state(ids["terminated"]),
                    "gone": await _state(f"{_PREFIX}gone"),
                    "live": await _state(ids["live"]),
                    "reopened": await _state(ids["reopened"]),
                    "stale_settle": str(stale),
                    "settled": ",".join(sorted(sweep.settled)),
                }
                for name in ("live", "reopened"):
                    await handles[name].terminate(reason="test over")
                row = await pending_store.get_request(ids["terminated"])
                states["reason"] = str((row.answer if row else {}).get("reason", ""))
                return states

    states = asyncio.run(_run())
    assert states["terminated"] == "cancelled", states
    assert states["gone"] == "cancelled", states
    assert states["live"] == "waiting", "a question somebody can still answer was cancelled"
    assert states["reopened"] == "waiting", states
    assert states["stale_settle"] == "False", "a sweep settled a run it never examined"
    assert states["settled"] == f"{_PREFIX}gone,{_PREFIX}terminated", states
    assert "terminated" in states["reason"], states["reason"]


def _returning(client: Any) -> Any:
    """`connect` replaced by one that hands back the test environment's client."""

    async def _connect() -> Any:
        return client

    return _connect
