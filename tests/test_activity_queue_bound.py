"""Every core activity bounds the wait for a worker, not just the run once one has it.

`start_to_close_timeout` is the only timeout the durable layer used to set, and it is not a bound on
a call: it starts counting when a worker *picks the task up*, so a queue nobody polls — the
background fleet scaled to zero, a rolling update, a queue named in config but served by no pod —
is an activity that never times out and a workflow that never ends. Most of these workflows are
Temporal Schedules under `ScheduleOverlapPolicy.SKIP`, so one wedged run skips every subsequent
fire of that job family, indefinitely, and a skipped fire is an error nowhere.

**The rule is stated over `durable/` and stops there on purpose.** A connector bundle schedules
onto its own queue, where a wait genuinely is backpressure — a CREST search holds its slot for
hours, so the next one queued behind it is working as designed. On core's `background-jobs` queue a
wait past the configured bound means a missing worker rather than a busy one, which is the state
this bound exists to make loud.

Two tests, deliberately of different kinds. The AST walk is the one that scales: it holds the rule
over every present and future call site, so the next durable job cannot be written without the
bound. The Temporal run is the one that proves the rule *does* something — that a bounded call
against an unserved queue fails, and fails with the timeout this is about, rather than merely
carrying an argument nobody checked.
"""

import ast
import asyncio
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

# This module drives a real workflow, so Temporal's sandbox re-imports it when validating that
# workflow — and `chemclaw.core.config` plus the test harness execute what the sandbox forbids.
# Passing them through is the established pattern (`tests/test_orchestrator.py` says why at length).
with workflow.unsafe.imports_passed_through():
    from temporalio.client import Client
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.note_index import NoteReindexWorkflow
    from tests.temporal_env import pydantic_client, start_env_or_skip

_DURABLE = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable"

# The two ways a call can bound its queue wait. `schedule_to_start_timeout` is the general one
# (`durable/publish.py::queue_wait_timeout`); `schedule_to_close_timeout` is stricter — it caps
# every attempt together — and `durable/notify.py` passes it deliberately for its best-effort
# push-back. Either satisfies the invariant this file exists for, which is that the wait is bounded
# at all.
_QUEUE_BOUNDS = {"schedule_to_start_timeout", "schedule_to_close_timeout"}


def _dispatch_calls() -> list[tuple[str, set[str]]]:
    """Every `workflow.execute_activity`/`.start_activity` under `durable/`, with its keywords.

    `execute_local_activity` is deliberately not walked: a local activity runs inside the workflow
    worker's own task, is never dispatched to a queue, and Temporal rejects a schedule-to-start
    timeout on one.

    **The receiver is matched, not only the method name**, and that is not defensive tidying: the
    walk used to accept any object with an `execute_activity` attribute, and
    `durable/interceptor.py` — a Temporal *worker* interceptor — delegates down its chain with
    `self.next.execute_activity(input)`. That call schedules nothing; it is the SDK handing an
    already-dispatched activity to the next link, and there is no queue wait to bound. Left
    unnarrowed the rule reported it as an offender, which would have been closed either by an
    exemption list or by passing a meaningless argument — both of which teach the next reader that
    the rule is negotiable. Every real dispatch in this tree is written `workflow.execute_activity`
    (measured: 31 of 31), which is the API this rule is about.
    """
    calls: list[tuple[str, set[str]]] = []
    for path in sorted(_DURABLE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {"execute_activity", "start_activity"}:
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "workflow"):
                continue
            calls.append((f"{path.name}:{node.lineno}", {kw.arg for kw in node.keywords if kw.arg}))
    return calls


def test_every_durable_activity_call_bounds_the_queue_wait() -> None:
    """No activity in the durable layer may be scheduled with only a start-to-close budget.

    The floor is asserted beside the rule, because a structural test that matches nothing passes.
    Narrowing the walk to `workflow.`-receiver calls is exactly the edit that could silently empty
    it, so the count it must not fall below is stated here rather than trusted.
    """
    calls = _dispatch_calls()
    assert len(calls) >= 30, (
        f"the walk found only {len(calls)} dispatch sites under durable/, which is fewer than this "
        "tree has ever had — the matcher has stopped seeing them and this rule is now vacuous"
    )
    unbounded = [where for where, passed in calls if not passed & _QUEUE_BOUNDS]
    assert unbounded == []


def test_an_activity_nobody_polls_fails_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow whose activity queue is unserved fails on the queue bound, and says so.

    The worker registers the workflow and **not** its activity, which is what a fleet with no
    background worker looks like from the server's side: the activity task is dispatched and never
    claimed. Before the bound this run stayed RUNNING forever; the assertion is both that it ends
    and that it ends on `SCHEDULE_TO_START`, since a start-to-close expiry would mean the test had
    proved something else.
    """
    monkeypatch.setattr(settings, "activity_queue_wait_seconds", 5.0)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[NoteReindexWorkflow],
            ):
                with pytest.raises(WorkflowFailureError) as failure:
                    await client.execute_workflow(
                        NoteReindexWorkflow.run,
                        id="unserved-activity-queue",
                        task_queue=settings.background_task_queue,
                    )
        cause = failure.value.cause
        assert isinstance(cause, ActivityError)
        timeout = cause.cause
        assert isinstance(timeout, TemporalTimeoutError)
        assert timeout.type is TimeoutType.SCHEDULE_TO_START

    asyncio.run(_run())
