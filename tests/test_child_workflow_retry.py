"""A failed durable parent must be able to re-run, so its children must be re-startable.

`ConnectorJobWorkflow` and `TemplateWorkflow` run under deterministic ids with
`ALLOW_DUPLICATE_FAILED_ONLY`, so a failed run may re-execute. Their children use
`REJECT_DUPLICATE` under an id that includes the parent's run id, so a re-execution gets fresh
child ids while a second start within one execution is still a bug. Driven with
`temporalio.workflow`'s ambient functions stubbed, because the properties are the arguments handed
to `execute_child_workflow`. The retry bound on those starts is pinned here too.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

from chemclaw.core.config import settings
from chemclaw.durable.connector_job import (
    ConnectorJobInput,
    ConnectorJobResult,
    ConnectorJobWorkflow,
)
from chemclaw.durable.template_activities import ResolvedJob, StepIdentity
from chemclaw.durable.template_job import TemplateWorkflow
from chemclaw.templates.manifest import JobStep

_PARENT_ID = "bo-start_optimization_campaign-deadbeef"
_RESULT = ConnectorJobResult(summary="campaign finished after 9 evaluation(s)", data={"best": 1})

_JOB = ConnectorJobInput(
    connector="bo",
    job="start_optimization_campaign",
    workflow="BoCampaignWorkflow",
    task_queue="connector-bo",
    payload={"objective_name": "solubility_max"},
    rationale="the Tuesday batch stalled at 60%",
    requested_by="oid-42",
)


class _Info:
    """The two fields of `workflow.info()` a child id is built from."""

    def __init__(self, run_id: str) -> None:
        self.workflow_id = _PARENT_ID
        self.run_id = run_id


def _stub_ambient(monkeypatch: pytest.MonkeyPatch, run_id: str) -> list[dict[str, Any]]:
    """Run the workflow bodies outside Temporal, capturing every child start they issue.

    Patched on `temporalio.workflow` itself, so both workflow modules see one stub and the captured
    ids are those the SDK would receive.
    """
    starts: list[dict[str, Any]] = []

    async def _child(*args: Any, **kwargs: Any) -> ConnectorJobResult:
        starts.append(kwargs)
        return _RESULT

    async def _activity(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(workflow, "info", lambda: _Info(run_id))
    monkeypatch.setattr(workflow, "now", lambda: datetime(2026, 8, 1, tzinfo=UTC))
    monkeypatch.setattr(workflow, "execute_child_workflow", _child)
    monkeypatch.setattr(workflow, "execute_activity", _activity)
    return starts


def _connector_job_child(monkeypatch: pytest.MonkeyPatch, run_id: str) -> dict[str, Any]:
    """The single child start `ConnectorJobWorkflow` issues on the execution `run_id`."""
    starts = _stub_ambient(monkeypatch, run_id)
    assert asyncio.run(ConnectorJobWorkflow().run(_JOB)) == _RESULT
    (start,) = starts
    return start


def _template_job_step_child(
    monkeypatch: pytest.MonkeyPatch, run_id: str, step_id: str
) -> dict[str, Any]:
    """The child start `TemplateWorkflow`'s `job` step issues on the execution `run_id`."""
    starts = _stub_ambient(monkeypatch, run_id)

    async def _resolve(*args: Any, **kwargs: Any) -> ResolvedJob:
        return ResolvedJob(
            connector="bo",
            job="start_optimization_campaign",
            workflow="BoCampaignWorkflow",
            task_queue="connector-bo",
            publish_to_graph=False,
            payload={"objective_name": "solubility_max"},
        )

    monkeypatch.setattr(workflow, "execute_local_activity", _resolve)
    step = JobStep(id=step_id, kind="job", job="start_optimization_campaign", arguments={})
    identity = StepIdentity(actor="chemist-1", roles=[], correlation_id="template-run-1")
    asyncio.run(TemplateWorkflow()._run_job_step(step, {}, identity, timedelta(seconds=30)))
    (start,) = starts
    return start


def test_a_re_executed_connector_job_starts_its_child_under_a_free_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-executed connector job starts its child under a free id.

    Two executions of one workflow id must not collide on the child's id, or the retry fails with
    "already started" before doing any work.
    """
    first = _connector_job_child(monkeypatch, "run-a")
    second = _connector_job_child(monkeypatch, "run-b")

    assert first["id"] != second["id"], (
        f"both executions start the child as {first['id']!r}; under REJECT_DUPLICATE the second "
        "run dies with WorkflowAlreadyStartedError before doing any work"
    )
    # Still traceable to the parent it belongs to — the reason the id was derived at all.
    assert first["id"].startswith(f"{_PARENT_ID}-") and first["id"].endswith("-run")


def test_a_connector_job_child_is_named_the_same_way_twice_within_one_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child id is replay-stable: a function of the execution, never of when it is computed.

    `run_id` comes from history, so it is stable within a run and differs across runs; a uuid or a
    clock would name a different child on replay.
    """
    assert (
        _connector_job_child(monkeypatch, "run-a")["id"]
        == _connector_job_child(monkeypatch, "run-a")["id"]
    )


def test_the_connector_job_child_still_rejects_a_duplicate_within_one_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant the original policy wanted, kept rather than traded away for the retry."""
    assert (
        _connector_job_child(monkeypatch, "run-a")["id_reuse_policy"]
        is WorkflowIDReusePolicy.REJECT_DUPLICATE
    )


def test_a_re_executed_template_run_can_restart_a_step_that_already_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-executed template run can restart a step that already succeeded.

    `TemplateWorkflow` re-runs its steps from the beginning, so a previously successful step's child
    id must be free too, or the run never reaches the failed step.
    """
    first = _template_job_step_child(monkeypatch, "run-a", "compute")
    second = _template_job_step_child(monkeypatch, "run-b", "compute")

    assert first["id"] != second["id"], (
        f"both executions start step 'compute' as {first['id']!r}; a template whose later step "
        "failed can never be re-run, because its earlier successful steps hold their ids"
    )
    assert first["id"].startswith(f"{_PARENT_ID}-") and first["id"].endswith("-compute")


def test_two_steps_of_one_template_execution_still_get_distinct_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two steps of one template execution still get distinct ids.

    Guards the wrong fix of replacing the step id with the run id rather than adding to it.
    """
    first = _template_job_step_child(monkeypatch, "run-a", "screen")
    second = _template_job_step_child(monkeypatch, "run-a", "confirm")
    assert first["id"] != second["id"]
    assert first["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE


def test_the_template_job_step_child_is_not_retried_at_the_workflow_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template `job` step is not retried at the child-workflow boundary.

    Temporal matches `non_retryable_error_types` against the outermost failure, so `BAD_DATA_RETRY`
    classifies nothing at a child boundary and degrades to plain retries; one deterministic error
    would rerun the whole connector job (a failed run caches nothing).
    """
    start = _template_job_step_child(monkeypatch, "run-a", "compute")

    attempts = start["retry_policy"].maximum_attempts
    assert attempts == 1, (
        f"a template `job` step starts the connector-job wrapper with maximum_attempts={attempts}; "
        "one deterministic bad-data failure therefore costs that many full connector-job "
        "executions instead of 1"
    )


def test_both_paths_into_the_connector_job_wrapper_agree_on_its_retry_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths into the connector-job wrapper agree on its retry bound.

    Pinned together so an edit to either start has to answer for the other.
    """
    template = _template_job_step_child(monkeypatch, "run-a", "compute")
    direct = _connector_job_child(monkeypatch, "run-a")
    assert template["retry_policy"].maximum_attempts == direct["retry_policy"].maximum_attempts == 1


def test_the_template_gives_the_wrapper_more_time_than_it_gives_its_own_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template gives the wrapper more time than the wrapper gives its own child.

    After its child returns, `ConnectorJobWorkflow` still records, publishes and pushes back. A
    workflow execution timeout is not delivered to workflow code, so with equal ceilings the
    wrapper's `_notify_failure` never runs and the job ends TIMED_OUT in silence.
    """
    start = _template_job_step_child(monkeypatch, "run-a", "compute")

    child_ceiling = timedelta(seconds=settings.connector_job_timeout_seconds)
    headroom = start["execution_timeout"] - child_ceiling

    assert headroom > timedelta(0), (
        f"the wrapper is bounded at {start['execution_timeout']} against a child ceiling of "
        f"{child_ceiling}: it expires first, and its failure push-back is never reached"
    )
    # Enough for the four post-child steps, each one activity's worth of wall clock.
    assert headroom >= timedelta(seconds=settings.activity_timeout_seconds * 4)
