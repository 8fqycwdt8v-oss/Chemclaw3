"""Every core activity bounds the wait for a worker, not just the run once one has it.

`start_to_close_timeout` is the only timeout the durable layer used to set, and it is not a bound on
a call: it starts counting when a worker *picks the task up*, so a queue nobody polls — the
background fleet scaled to zero, a rolling update, a queue named in config but served by no pod —
is an activity that never times out and a workflow that never ends. Most of these workflows are
Temporal Schedules under `ScheduleOverlapPolicy.SKIP`, so one wedged run skips every subsequent
fire of that job family, indefinitely, and a skipped fire is an error nowhere.

**The rule now covers connector bundles too, at their own scale.**
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait` scoped it to `durable/` and argued
the exclusion: on a bundle queue a wait genuinely is backpressure, since a CREST search holds its
slot for hours and the next one behind it is working as designed. That argument is right and it is
not an argument for *no* bound — which is what the three bundles shipped, leaving a queued job
bounded only by the parent wrapper's five-hour execution ceiling, a failure delivered to no
workflow code and naming neither the queue nor the reason. So the bundles pass
`connector_queue_wait_timeout()` instead of core's hour: generous enough that the measured
backpressure (p50 ~1.04 h, p95 ~1.98 h on `connector-calc` at target load) passes through, tight
enough that "nothing is serving this queue" stops looking like "everything is busy".

The walk covers both trees for one reason: the failure it exists to prevent is a *new* call site
written without a bound, and a bundle added next year is exactly that. Three assertions naming
today's three files would have said nothing about the fourth.

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

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# Every tree whose workflows dispatch activities onto a task queue, with the floor each must not
# fall below. `connectors/` is walked whole rather than as `connectors/*/workflows.py`: a bundle
# that puts a workflow anywhere else in its package is the same rule and the same failure.
_WORKFLOW_TREES = {"durable": 30, "connectors": 7}

# The two ways a call can bound its queue wait. `schedule_to_start_timeout` is the general one
# (`durable/publish.py::queue_wait_timeout`); `schedule_to_close_timeout` is stricter — it caps
# every attempt together — and `durable/notify.py` passes it deliberately for its best-effort
# push-back. Either satisfies the invariant this file exists for, which is that the wait is bounded
# at all.
_QUEUE_BOUNDS = {"schedule_to_start_timeout", "schedule_to_close_timeout"}

# The two SDK calls that put an activity task on a queue. `execute_local_activity` is deliberately
# absent (see `_dispatch_calls`).
_DISPATCH_NAMES = {"execute_activity", "start_activity"}


def _dispatch_calls(tree: str) -> list[tuple[str, set[str]]]:
    """Every `workflow.execute_activity`/`.start_activity` under `src/chemclaw/<tree>`.

    Returns one `(file:line, keyword names)` pair per dispatched activity call.

    `execute_local_activity` is deliberately not walked: a local activity runs inside the workflow
    worker's own task, is never dispatched to a queue, and Temporal rejects a schedule-to-start
    timeout on one.

    **One shape is excluded, rather than one shape allow-listed**, and the difference is what makes
    the rule hold for code nobody has written yet. `durable/interceptor.py` — a Temporal *worker*
    interceptor — delegates down its chain with `self.next.execute_activity(input)`. That call
    schedules nothing; it is the SDK handing an already-dispatched activity to the next link, and
    there is no queue wait to bound, so it is the one receiver this walk skips.

    The walk used to do the opposite: it required the receiver to be the literal name `workflow`,
    on the grounds that every real dispatch in this tree is written that way (measured: 31 of 31,
    still true). That is a fact about today's tree, and this rule exists for tomorrow's. Measured
    against the merged tree, three ordinary spellings walked straight past it, each carrying only a
    `start_to_close_timeout` and each leaving the suite green: `from temporalio import workflow as
    wf` then `wf.execute_activity(...)`; `from temporalio.workflow import execute_activity` then a
    bare `execute_activity(...)`, which is an `ast.Name` and never reached the receiver check at
    all; and a site under a `durable/` subpackage, which `glob` does not descend into. The floor
    below cannot see any of them — an unmatched site does not raise the count — so it guards
    against sites disappearing and is simply orthogonal to a site that was never seen.
    """
    calls: list[tuple[str, set[str]]] = []
    for path in sorted((_SRC / tree).rglob("*.py")):
        module = ast.parse(path.read_text())
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr not in _DISPATCH_NAMES:
                    continue
                # `self.next.execute_activity(...)`: the interceptor chain, not a dispatch.
                receiver = func.value
                if (
                    isinstance(receiver, ast.Attribute)
                    and receiver.attr == "next"
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "self"
                ):
                    continue
            elif isinstance(func, ast.Name):
                # `from temporalio.workflow import execute_activity` — a bare call.
                if func.id not in _DISPATCH_NAMES:
                    continue
            else:
                continue
            where = path.relative_to(_SRC).as_posix()
            calls.append((f"{where}:{node.lineno}", {kw.arg for kw in node.keywords if kw.arg}))
    return calls


@pytest.mark.parametrize(("tree", "floor"), sorted(_WORKFLOW_TREES.items()))
def test_every_dispatched_activity_call_bounds_the_queue_wait(tree: str, floor: int) -> None:
    """No activity anywhere may be scheduled with only a start-to-close budget.

    Parametrised over the trees rather than written twice, so the connector bundles are held to the
    rule by the same walk that holds core to it — the point of a scan over three assertions is that
    it also covers the bundle nobody has written yet.

    The floor is asserted beside the rule, because a structural test that matches nothing passes.
    Narrowing the walk to `workflow.`-receiver calls is exactly the edit that could silently empty
    it, so the count it must not fall below is stated here rather than trusted.
    """
    calls = _dispatch_calls(tree)
    assert len(calls) >= floor, (
        f"the walk found only {len(calls)} dispatch sites under {tree}/, which is fewer than this "
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
