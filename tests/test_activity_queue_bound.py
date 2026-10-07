"""Every core activity bounds the wait for a worker, not just the run once one has it.

`start_to_close_timeout` counts from pickup, so an unpolled queue would leave an activity, and a
Schedule under `SKIP` overlap, wedged forever. Core activities bound the wait at core's scale;
connector bundles use `connector_queue_wait_timeout()`, generous enough for real backpressure but
finite, so "nothing serves this queue" is distinguishable from "busy".

An AST walk over `durable/` and `connectors/` holds the rule for every present and future call
site; a Temporal run proves a bounded call against an unserved queue fails on that bound.
"""

import ast
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

# Every tree whose workflows dispatch activities, with the floor of sites each walk must find.
# `connectors/` is walked whole, since a workflow anywhere in a bundle is under the same rule. The
# floor sits at the current count so a walk that silently stops finding sites fails.
_WORKFLOW_TREES = {"durable": 41, "connectors": 7}

# The two ways a call can bound its queue wait. `schedule_to_start_timeout` is the one to use;
# `schedule_to_close_timeout` also bounds the wait but caps all attempts together, so it is
# accepted here and not recommended.
_QUEUE_BOUNDS = {"schedule_to_start_timeout", "schedule_to_close_timeout"}

# Every SDK call that puts an activity task on a queue, including the `_method`/`_class` forms
# for class-bound activities. `execute_local_activity` is deliberately absent (see
# `_dispatch_calls`).
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

    `execute_local_activity` is not walked: a local activity never waits on a queue, and Temporal
    rejects a schedule-to-start timeout on one. Any receiver is matched (aliases, bare imports,
    subpackages) except `self.next`, which is a worker interceptor delegating an already-dispatched
    activity.
    """
    calls: list[tuple[str, set[str]]] = []
    for path in sorted((_SRC / tree).rglob("*.py")):
        calls.extend(_dispatch_calls_in(path.read_text(), path.relative_to(_SRC).as_posix()))
    return calls


def _dispatch_calls_in(source: str, where: str) -> list[tuple[str, set[str]]]:
    """The walk itself, over one module's text — so a test can drive it on a spelling nobody wrote.

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

    Parametrised over the trees so bundles and core are held by one walk. The floor is asserted too,
    because a structural test that matches nothing passes.
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

    The `_method` form is the ordinary way to dispatch a class-bound activity, so the walk must see
    it. Driven on source text, with a bounded twin so a matcher that flags everything fails too.
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


async def test_an_activity_nobody_polls_fails_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow whose activity queue is unserved fails on the queue bound, and says so.

    The worker registers the workflow but not its activity, so the task is never claimed. The run
    must end, and on `SCHEDULE_TO_START` specifically.
    """
    monkeypatch.setattr(settings, "activity_queue_wait_seconds", 5.0)

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


@pytest.mark.parametrize("ceiling", [3600.0, 25200.0, 86400.0])
def test_the_job_ceiling_funds_exactly_one_worst_case_attempt_at_any_setting(
    monkeypatch: pytest.MonkeyPatch, ceiling: float
) -> None:
    """One attempt fits, a second cannot, and no ceiling changes it — so nothing may claim it does.

    `connector_queue_wait_timeout` is derived as `C - w - a`, so one attempt's `q + w` is `C - a` at
    any ceiling and a second attempt has only `a`. Parametrised over ceilings so a re-derivation as
    a fraction of `C` is caught.
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
    """The twin of the rule above, for `fan_out` children.

    `fan_out_queue_wait_timeout` is derived as `C - w - a`, so a child's own `SCHEDULE_TO_START`
    expiry is reachable inside `fan_out_child_timeout_seconds` (and reported with its cause) at
    every ceiling.
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

    Asserted on the source: the defect is a keyword argument's value, which a green run cannot
    reveal.
    """
    children = {
        "durable/report_workflow.py": "ReportSectionWorkflow",
        "durable/memory_jobs.py": "PublishNoteWorkflow",
    }
    for module, child in children.items():
        text = (_SRC / module).read_text()
        after = text.split(f"class {child}:", 1)[1]
        # The class body ends at the first line back in column 0, so another function's queue bound
        # cannot satisfy the assertion.
        body = re.split(r"\n(?=\S)", after, maxsplit=1)[0]
        assert "schedule_to_start_timeout=fan_out_queue_wait_timeout()" in body, (
            f"{child} ({module}) does not bound its queue wait with fan_out_queue_wait_timeout(); "
            "core's hour equals the fan-out ceiling, so its degradation path is unreachable"
        )
        assert "schedule_to_start_timeout=queue_wait_timeout()" not in body


# --- a sequence of dispatches under one ceiling --------------------------------------------------


class _StubbedRun:
    """Just enough of a workflow context to drive `BoCampaignWorkflow._queue_wait` and a clock.

    The method reads `workflow.info()` and `workflow.now()` only, so stubbing those drives the real
    arithmetic without a broker.
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

        The worst case a ceiling must fund: every dispatch waits its full allowance and then runs
        its full budget.
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

    A one-round `BoCampaignWorkflow` runs six activities; giving each the full per-dispatch queue
    wait would overrun the ceiling with an uninformative `WorkflowExecutionTimedOut`. All six must
    fit: the remaining budget is shared between the dispatches still to come (`dispatches_left`).
    Parametrised over ceilings because the property is structural; the lowest is the shipped value.
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

    At a low ceiling over few dispatches the share exceeds the queue-wide bound, so the `min` keeps
    a campaign with no worker from waiting longer than any other job on the queue.
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

    `run` catches this and ends the campaign with its history and best point, instead of an
    execution timeout firing mid-activity.
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

    The queue-wide bound is the whole answer. Driven a year into the run so a stray elapsed-time
    subtraction would fail.
    """
    run = _StubbedRun(monkeypatch, None)
    run.now = run.started + timedelta(days=365)
    assert not run.campaign._cannot_afford_another_dispatch()
    assert run.campaign._queue_wait() == connector_queue_wait_timeout()


def test_a_remaining_budget_that_cannot_fund_an_attempt_answers_none() -> None:
    """`None` rather than a zero or negative timedelta, because those are not bounds.

    Temporal would reject or instantly expire a non-positive `schedule_to_start_timeout`, which
    reads as "queue unserved"; the caller must decide differently.
    """
    overhead = settings.activity_timeout_seconds
    assert remaining_queue_wait_timeout(timedelta(seconds=300 + overhead + 1), 300.0) == timedelta(
        seconds=1
    )
    assert remaining_queue_wait_timeout(timedelta(seconds=300 + overhead), 300.0) is None
    assert remaining_queue_wait_timeout(timedelta(seconds=0), 300.0) is None


@pytest.mark.parametrize("n_rounds", [25, 100, settings.bo_max_rounds])
def test_a_long_campaign_dispatches_its_seed_instead_of_refusing_it(
    monkeypatch: pytest.MonkeyPatch, n_rounds: int
) -> None:
    """A campaign with a large round count must run, not fail before doing any work.

    Dividing the remaining ceiling by the worst-case total dispatch count (`3n + 3`) made the share
    too small to fund even the first dispatch at large `n`. The share may narrow a wait but must
    never refuse one, since each dispatch is measured against what is left. Asserted as a bound the
    campaign can plainly afford.
    """
    run = _StubbedRun(monkeypatch, settings.connector_job_timeout_seconds, n_rounds=n_rounds)

    wait = run.campaign._queue_wait()

    assert wait > timedelta(0)
    # The counterfactual that makes the assertion mean something: the budget this dispatch was
    # refused out of funds it many times over.
    affordable = settings.connector_job_timeout_seconds - (
        settings.bo_activity_timeout_seconds + settings.activity_timeout_seconds
    )
    assert affordable > 0
    assert 3 * n_rounds + 3 > affordable / (
        settings.bo_activity_timeout_seconds + settings.activity_timeout_seconds
    ), "this round count no longer produces a share below one attempt, so the test is vacuous"


def test_a_shared_queue_wait_never_falls_below_the_configured_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing must not hand out a wait too short to survive a worker that is merely slow.

    The share `C/n - w - a` shrinks with the round count while worker gaps do not, so it is floored
    at `bo_queue_wait_floor_seconds`. Both real bounds still apply above the floor.
    """
    run = _StubbedRun(monkeypatch, settings.connector_job_timeout_seconds, n_rounds=10)
    share = remaining_queue_wait_timeout(
        timedelta(seconds=settings.connector_job_timeout_seconds / dispatches_left(10, True)),
        settings.bo_activity_timeout_seconds,
    )
    assert share is not None and share < timedelta(seconds=settings.bo_queue_wait_floor_seconds), (
        "the default spec's share is no longer under the floor, so this test is vacuous"
    )

    assert run.campaign._queue_wait() == timedelta(seconds=settings.bo_queue_wait_floor_seconds)

    # And the floor is a floor, not an override: a campaign down to its last few minutes is still
    # bounded by what it can afford.
    run.now = run.started + timedelta(
        seconds=settings.connector_job_timeout_seconds
        - settings.bo_activity_timeout_seconds
        - settings.activity_timeout_seconds
        - 60
    )
    run.campaign._dispatches_left = dispatches_left(10, seeding=False)
    assert run.campaign._queue_wait() == timedelta(seconds=60)


def test_the_floored_share_still_fits_the_ceiling_every_dispatch_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor may not buy a usable wait at the cost of the bound it sits inside.

    On the longest accepted campaign, every dispatch waits and runs its worst case until the
    campaign refuses to dispatch; the total must still fit the ceiling less one activity's overhead.
    """
    ceiling = settings.connector_job_timeout_seconds
    run = _StubbedRun(monkeypatch, ceiling, n_rounds=settings.bo_max_rounds)

    dispatched = 0
    while True:
        try:
            run.dispatch()
        except CampaignBudgetSpent:
            break
        dispatched += 1
        run.campaign._dispatches_left = max(run.campaign._dispatches_left, 1)
        assert dispatched < 10_000, "the recurrence never ends"

    assert dispatched > 1, "the campaign refused before it had run anything"
    assert run.spent <= ceiling - settings.activity_timeout_seconds, run.spent


def test_a_campaign_that_stops_for_budget_can_still_write_its_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one write `resume_campaign` keys on must not be refused by a divisor.

    A campaign stopping for budget reaches the terminal `record_campaign_run` with dispatches still
    counted; if affordability were read off the share, that write — and the `bo_campaigns` row
    `resume_campaign` keys on — would always be refused. 1,000 s left must fund a 670 s wait plus a
    300 s attempt.
    """
    ceiling = settings.connector_job_timeout_seconds
    run = _StubbedRun(monkeypatch, ceiling, n_rounds=1)
    monkeypatch.setattr(settings, "bo_activity_timeout_seconds", 300.0)
    monkeypatch.setattr(settings, "activity_timeout_seconds", 30.0)
    run.now = run.started + timedelta(seconds=ceiling - 1000)
    # As `run` leaves the loop: re-synced for a round that then did not happen.
    run.campaign._dispatches_left = dispatches_left(40, seeding=False)

    assert run.campaign._queue_wait() == timedelta(seconds=670)
