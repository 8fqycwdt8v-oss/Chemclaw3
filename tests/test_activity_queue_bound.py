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
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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

    from chemclaw.connectors.bo.workflows import (
        BoCampaignWorkflow,
        CampaignBudgetSpent,
        dispatches_left,
    )
    from chemclaw.core.config import settings
    from chemclaw.durable.note_index import NoteReindexWorkflow
    from chemclaw.durable.publish import (
        connector_queue_wait_timeout,
        fan_out_queue_wait_timeout,
        remaining_queue_wait_timeout,
    )
    from tests.temporal_env import pydantic_client, start_env_or_skip

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"

# Every tree whose workflows dispatch activities onto a task queue, with the floor each must not
# fall below. `connectors/` is walked whole rather than as `connectors/*/workflows.py`: a bundle
# that puts a workflow anywhere else in its package is the same rule and the same failure.
_WORKFLOW_TREES = {"durable": 30, "connectors": 7}

# The two ways a call can bound its queue wait. `schedule_to_start_timeout` is the general one
# (`durable/publish.py::queue_wait_timeout`, and `light_write_queue_wait_timeout` for the
# end-of-job writes); `schedule_to_close_timeout` is stricter — it caps every attempt together.
# **No call site passes the second one today.** The two small writes that did were moved off it,
# measured: it is a *total*, so on a queue they do not control it was spent on the wait and it
# capped all five attempts together. It stays accepted here because it does bound the wait, which
# is the invariant this file exists for; it is not the bound to reach for.
_QUEUE_BOUNDS = {"schedule_to_start_timeout", "schedule_to_close_timeout"}

# Every SDK call that puts an activity task on a queue. `execute_local_activity` is deliberately
# absent (see `_dispatch_calls`).
#
# **The `_method`/`_class` forms are the ordinary spelling for a class-bound activity, and this set
# held only the two bare ones** — so `workflow.execute_activity_method(Cls.act, …)` with nothing
# but a start-to-close budget walked straight past the scan, which is the same evasion the
# receiver check was widened to close (`_dispatch_calls` records that measurement).
# `test_the_scan_sees_a_class_bound_dispatch` drives one rather than trusting this list.
_DISPATCH_NAMES = {
    "execute_activity",
    "execute_activity_method",
    "execute_activity_class",
    "start_activity",
    "start_activity_method",
    "start_activity_class",
}


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
        calls.extend(_dispatch_calls_in(path.read_text(), path.relative_to(_SRC).as_posix()))
    return calls


def _dispatch_calls_in(source: str, where: str) -> list[tuple[str, set[str]]]:
    """The walk itself, over one module's text — so a test can drive it on a spelling nobody wrote.

    Split out from the tree walk for one reason: the failure this file guards is a *new* call site
    written without a bound, and the only honest way to show the matcher sees a spelling is to hand
    it that spelling. Asserting the names against `dir(temporalio.workflow)` instead would say the
    names exist, not that this walk matches them.

    Args:
        source: One module's text.
        where: How a match in it should be named in the failure message.

    Returns:
        One `(where:line, keyword names)` pair per dispatched activity call.
    """
    calls: list[tuple[str, set[str]]] = []
    for node in ast.walk(ast.parse(source)):
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


def test_the_scan_sees_a_class_bound_dispatch() -> None:
    """The `_method`/`_class` spellings, driven rather than listed.

    `_DISPATCH_NAMES` held `execute_activity` and `start_activity` only, while
    `temporalio.workflow` also exports `execute_activity_method`, `execute_activity_class`,
    `start_activity_method` and `start_activity_class` — and the `_method` form is the *ordinary*
    way to dispatch a class-bound activity rather than an exotic one. So the walk above could be
    passed by writing the next durable job in the shape the SDK's own documentation shows, which
    makes the rule advisory exactly where it is meant to be structural.

    Driven on source text rather than asserted against `dir(temporalio.workflow)`: the question is
    whether *this walk* matches the spelling, and a name list that agrees with itself is what the
    receiver check already learned not to trust. The bounded twin is checked in the same breath, so
    a matcher that flagged everything would not pass this either.
    """
    unbounded = """
class Job:
    async def run(self) -> None:
        await workflow.execute_activity_method(
            Worker.record, start_to_close_timeout=timedelta(seconds=30)
        )
"""
    bounded = """
class Job:
    async def run(self) -> None:
        await workflow.start_activity_class(
            Worker,
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_start_timeout=timedelta(seconds=60),
        )
"""
    seen = _dispatch_calls_in(unbounded, "synthetic.py")
    assert len(seen) == 1, "a class-bound dispatch is invisible to the walk"
    assert not seen[0][1] & _QUEUE_BOUNDS, "and this one is the unbounded shape the rule refuses"

    ok = _dispatch_calls_in(bounded, "synthetic.py")
    assert len(ok) == 1
    assert ok[0][1] & _QUEUE_BOUNDS


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


@pytest.mark.parametrize("ceiling", [3600.0, 25200.0, 86400.0])
def test_the_job_ceiling_funds_exactly_one_worst_case_attempt_at_any_setting(
    monkeypatch: pytest.MonkeyPatch, ceiling: float
) -> None:
    """One attempt fits, a second cannot, and no ceiling changes it — so nothing may claim it does.

    `Settings._the_job_ceiling_covers_the_activity_it_bounds` said in its own docstring that it
    keeps `BAD_DATA_RETRY` alive, "so `activity_max_attempts` is a number that can never be
    reached" is the thing it prevents. Measured at the shipped defaults it prevents no such thing:
    longest 15,000 s, ceiling 25,200 s, queue wait 10,170 s, one worst-case attempt 25,170 s, 30 s
    left over against `activity_max_attempts=5`.

    And it is structural rather than a badly chosen value, which is why this is parametrized over
    three ceilings a decade apart instead of asserting the shipped numbers. A bundle activity's
    attempt costs `q + w`, and `connector_queue_wait_timeout` is derived as `C - w - a` precisely so
    that composite fits by construction — so `q + w` is `C - a` whatever `C` is, and the second
    attempt has `a` to spend. Anything that re-derives the queue wait as a fraction of the ceiling
    (the shape `connector_queue_wait_timeout` says was reverted for making `q` grow with `C`) moves
    this ratio, which is the drift worth catching.
    """
    monkeypatch.setattr(settings, "connector_job_timeout_seconds", ceiling)
    longest, _budget = settings.longest_bundle_activity
    overhead = settings.activity_timeout_seconds
    one_attempt = connector_queue_wait_timeout().total_seconds() + longest

    assert one_attempt == ceiling - overhead
    assert one_attempt <= ceiling, "the ceiling must fund one worst-case attempt"
    assert 2 * one_attempt > ceiling, (
        "a second full-length attempt fits, so the ceiling now funds retries and "
        "`_the_job_ceiling_covers_the_activity_it_bounds` may say so"
    )


@pytest.mark.parametrize("ceiling", [3600.0, 7200.0, 86400.0])
def test_the_fan_out_ceiling_funds_one_worst_case_child_at_any_setting(
    monkeypatch: pytest.MonkeyPatch, ceiling: float
) -> None:
    """The twin of the rule above, on the pair that shipped without it.

    `fan_out_child_timeout_seconds` bounds a report section and a note publish, and both children
    passed core's flat `queue_wait_timeout()` — 3,600 s — as their `schedule_to_start` under a
    3,600 s ceiling. So the composite a child may legally spend was `3,600 + 300 = 3,900` against
    3,600: the ceiling was *equal to the wait it had to contain*, and the child's own
    SCHEDULE_TO_START expiry — the failure `ReportSectionWorkflow`'s `except ActivityError`
    degrades on and `activity_failure_reason` names — could never be reached. Driven on the real
    broker at 1000:1 the child came back as a bare `ChildWorkflowError`, which `fan_out` drops
    without a cause.

    Parametrized over three ceilings rather than asserting the shipped numbers, for the reason its
    twin gives: `fan_out_queue_wait_timeout` is derived as `C - w - a`, so `q + w == C - a` at
    every ceiling and a re-derivation as a fraction of `C` is what would move it.
    """
    monkeypatch.setattr(settings, "fan_out_child_timeout_seconds", ceiling)
    longest, _budget = settings.longest_fan_out_activity
    overhead = settings.activity_timeout_seconds
    one_attempt = fan_out_queue_wait_timeout().total_seconds() + longest

    assert one_attempt == ceiling - overhead
    assert one_attempt < ceiling, (
        "the ceiling must strictly exceed one worst-case child, or the child's own "
        "schedule-to-start expiry is pre-empted by the parent's execution timeout"
    )


def test_every_fan_out_child_waits_on_the_fan_out_bound_not_on_cores_hour() -> None:
    """Each `fan_out` child's activity passes the derived wait, and the walk says which children.

    The composite above fits by construction only for a call site that *uses* the derived wait, and
    the defect this closes was precisely that two sites did not. Asserted against the source of the
    two child workflows rather than by driving them, because what is wrong in the broken shape is a
    keyword argument's value and nothing about a green run says which timeout was passed.
    """
    children = {
        "durable/report_workflow.py": "ReportSectionWorkflow",
        "durable/memory_jobs.py": "PublishNoteWorkflow",
    }
    for module, child in children.items():
        text = (_SRC / module).read_text()
        after = text.split(f"class {child}:", 1)[1]
        # The class body ends at the first line that starts in column 0 again — the next
        # decorator, def or class. Splitting on the decorator alone ran past the end of the
        # file when the child is the last decorated thing in its module, which would let a
        # *different* function's queue bound satisfy the assertion below.
        body = re.split(r"\n(?=\S)", after, maxsplit=1)[0]
        assert "schedule_to_start_timeout=fan_out_queue_wait_timeout()" in body, (
            f"{child} ({module}) does not bound its queue wait with fan_out_queue_wait_timeout(); "
            "core's hour equals the fan-out ceiling, so its degradation path is unreachable"
        )
        assert "schedule_to_start_timeout=queue_wait_timeout()" not in body


# --- a sequence of dispatches under one ceiling --------------------------------------------------


class _StubbedRun:
    """Just enough of a workflow context to drive `BoCampaignWorkflow._queue_wait` and a clock.

    The method under test reads exactly two things from the SDK — `workflow.info()` for the run's
    execution budget and start, and `workflow.now()` for where it is inside that budget — so a
    stub of those two drives the real arithmetic without a broker. Driving it on the real method
    rather than re-deriving the recurrence in the test is the point: a test that recomputed
    `min(queue_bound, remaining)` here would agree with itself forever.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, ceiling: float | None, n_rounds: int = 1
    ) -> None:
        """Start a campaign at t=0 under `ceiling`, or under no execution ceiling when None."""
        self.started = datetime(2026, 9, 12, tzinfo=UTC)
        self.now = self.started
        info = SimpleNamespace(
            execution_timeout=None if ceiling is None else timedelta(seconds=ceiling),
            workflow_start_time=self.started,
        )
        monkeypatch.setattr(workflow, "info", lambda: info)
        monkeypatch.setattr(workflow, "now", lambda: self.now)
        self.campaign = BoCampaignWorkflow()
        self.campaign._dispatches_left = dispatches_left(n_rounds, seeding=True)

    def dispatch(self) -> float:
        """Take the next activity's queue bound, spend the whole of it, then run the activity.

        The worst case, which is what a ceiling has to fund: every dispatch waits its full
        allowance and then takes its full start-to-close budget. A real campaign spends less on
        both, which is why the assertions below are inequalities.
        """
        wait = self.campaign._queue_wait().total_seconds()
        self.now += timedelta(seconds=wait + settings.bo_activity_timeout_seconds)
        return wait

    @property
    def spent(self) -> float:
        """Wall clock consumed since the campaign started, in seconds."""
        return (self.now - self.started).total_seconds()


@pytest.mark.parametrize("ceiling", [25200.0, 50400.0, 86400.0])
def test_a_campaigns_sequence_of_dispatches_fits_the_ceiling_they_share(
    monkeypatch: pytest.MonkeyPatch, ceiling: float
) -> None:
    """Six dispatches under one execution ceiling, and the sum has to fit inside it.

    **`connector_queue_wait_timeout` is derived so that `q + w` fits, singular.** Every bundle
    child passed it unchanged, and `BoCampaignWorkflow` runs six activities for a one-round
    campaign: propose the seed, evaluate it, propose the round, evaluate it, record the round,
    record the campaign. Measured at the shipped settings before this: `q` = 10,170 s and a `bo`
    activity's `w` = 300 s, so 6 x 10,470 = 62,820 s against a 25,200 s ceiling — 2.5x. Two fit,
    the third breaks it, and the overrun is a `WorkflowExecutionTimedOut`, which reaches no
    workflow code and names neither the queue nor the reason.

    Parametrised over three ceilings rather than asserting the shipped numbers, for the reason its
    two siblings above give: the property is structural, and a re-derivation that reintroduced a
    per-dispatch constant would pass at one ceiling by luck. The lowest is the shipped value; going
    below `longest_bundle_activity` would test a ceiling `Settings` refuses.

    **All six must fit, not merely "the sum is bounded".** Bounding each dispatch by the whole
    remaining budget is enough for the sum — but measured that way the first two take 10,170 s each
    of 25,200 and the campaign stops after three, having proposed a seed and evaluated it. Sharing
    what is left between the dispatches still to come is what makes the bound usable, and it is
    what the count in `dispatches_left` is for.
    """
    monkeypatch.setattr(settings, "connector_job_timeout_seconds", ceiling)
    run = _StubbedRun(monkeypatch, ceiling, n_rounds=1)

    waits = [run.dispatch() for _ in range(6)]

    assert run.spent <= ceiling - settings.activity_timeout_seconds, run.spent
    assert all(wait > 0 for wait in waits), waits
    # And the counterfactual, so a bound that had quietly gone flat could not pass: the same six
    # dispatches on the queue-wide constant alone overrun the ceiling they share.
    flat = connector_queue_wait_timeout().total_seconds() + settings.bo_activity_timeout_seconds
    assert 6 * flat > ceiling, (
        "the flat bound now fits six dispatches, so this test no longer discriminates"
    )


def test_a_campaign_never_waits_longer_than_the_queue_wide_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing the budget must not *lengthen* a wait, which on a short campaign it would.

    The queue-wide bound is `C - 15,000 - 30` (the fleet's longest bundle activity), and a share is
    `C/n - 300 - 30`, so which one binds depends on the ceiling: at the shipped 25,200 s over six
    dispatches the share is the tighter, and at a *low* ceiling over few dispatches the queue bound
    is. 20,000 s with three dispatches is the second case — share 6,337 s against a queue bound of
    4,970 s — and without the `min` a campaign whose worker is simply absent would sit 1,367 s
    longer than any other job on the same queue before saying so.
    """
    ceiling = 20000.0
    monkeypatch.setattr(settings, "connector_job_timeout_seconds", ceiling)
    run = _StubbedRun(monkeypatch, ceiling, n_rounds=0)
    queue_bound = connector_queue_wait_timeout()
    share = remaining_queue_wait_timeout(
        timedelta(seconds=ceiling / dispatches_left(0, seeding=True)),
        settings.bo_activity_timeout_seconds,
    )
    assert share is not None and share > queue_bound, (
        "this ceiling no longer produces a share above the queue bound, so the `min` is untested"
    )
    assert run.campaign._queue_wait() == queue_bound


def test_a_campaign_that_has_spent_its_ceiling_refuses_to_dispatch_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end of the recurrence is a stop, not a silent overrun.

    `run` catches this and ends the campaign with the history and the best point it has. The
    alternative — dispatch anyway — is the failure the whole bound exists to remove: the execution
    timeout fires mid-activity, is delivered to no workflow code, and the chemist is told nothing
    about a run that had real evaluations in it.
    """
    ceiling = settings.connector_job_timeout_seconds
    run = _StubbedRun(monkeypatch, ceiling)
    run.now = run.started + timedelta(seconds=ceiling - settings.bo_activity_timeout_seconds)
    assert run.campaign._cannot_afford_another_dispatch()
    with pytest.raises(CampaignBudgetSpent):
        run.campaign._queue_wait()


def test_a_campaign_with_no_execution_ceiling_keeps_the_queue_wide_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measured campaign gets no execution timeout, so there is no budget to spend down.

    `child_execution_timeout` returns None for a job that suspends on a person —
    `bo_measurement_deadline_days` is a fortnight and no wall-clock ceiling can contain it. There
    is then nothing to divide, and the queue-wide bound is the whole of the answer. Driven a year
    into the run so a stray elapsed-time subtraction could not pass this.
    """
    run = _StubbedRun(monkeypatch, None)
    run.now = run.started + timedelta(days=365)
    assert not run.campaign._cannot_afford_another_dispatch()
    assert run.campaign._queue_wait() == connector_queue_wait_timeout()


def test_a_remaining_budget_that_cannot_fund_an_attempt_answers_none() -> None:
    """`None` rather than a zero or negative timedelta, because those are not bounds.

    Temporal takes a `schedule_to_start_timeout` at face value: a non-positive one either fails
    validation or expires the activity the instant it is scheduled, and both read as "the queue is
    unserved" about a queue that is fine. The caller has to make a different decision, so the
    function has to be able to say something different.
    """
    overhead = settings.activity_timeout_seconds
    assert remaining_queue_wait_timeout(timedelta(seconds=300 + overhead + 1), 300.0) == timedelta(
        seconds=1
    )
    assert remaining_queue_wait_timeout(timedelta(seconds=300 + overhead), 300.0) is None
    assert remaining_queue_wait_timeout(timedelta(seconds=0), 300.0) is None
