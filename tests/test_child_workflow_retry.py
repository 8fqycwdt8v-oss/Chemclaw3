"""A failed durable parent must be able to re-run — which means its children must be re-startable.

One defect, two sites. `ConnectorJobWorkflow` and `TemplateWorkflow` are both launched under a
deterministic, payload-derived workflow id with `ALLOW_DUPLICATE_FAILED_ONLY`
(`connectors/jobs.py`, `templates/registry.py`), a policy whose entire purpose is that a *failed*
run may re-execute under the same id. Both then started their child under an id derived from the
parent's **workflow id** alone, with `REJECT_DUPLICATE` — which refuses a closed id regardless of
how it closed. So the second parent execution that the policy exists to permit died at its first
child start with `WorkflowAlreadyStartedError`, having done no work, and stayed dead until the
closed child aged out of namespace retention. The template path was worse: every step that had
*succeeded* the first time held its id too, so a retry could not reach the step that failed.

These tests drive the workflow bodies directly with `temporalio.workflow`'s ambient functions
stubbed, because the two properties in question are properties of the *arguments* the workflow
hands to `execute_child_workflow` — the id and the reuse policy — and a real Temporal server (which
this suite cannot download offline) would only be able to confirm them by actually failing a run
and retrying it. Stubbing the ambient module rather than the workflow's own code means the ids
under test are the ids the SDK would receive.

`REJECT_DUPLICATE` is asserted alongside, because the alternative fix — widening the child to
`ALLOW_DUPLICATE` — buys the retry by giving up the invariant the original policy wanted: one child
per parent execution, so a second start of the same id is a bug rather than a silent re-run. The
run id scopes that invariant to an execution instead of abandoning it, so both claims are pinned
here together or the next reader cannot tell which one the code is trying to hold.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

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

    Patched on `temporalio.workflow` itself rather than on a copy, so both workflow modules — which
    each did `from temporalio import workflow` — see one stub and the ids captured are the ids the
    SDK would have been handed. `monkeypatch` restores the module afterwards.
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
    """The defect itself: the second execution the parent's policy permits must be able to start.

    Two executions of one workflow id — which is exactly what `ALLOW_DUPLICATE_FAILED_ONLY` allows
    after a failure — must not collide on the child's id, or the retry fails before doing any work
    and the user is told "already started" instead of getting their re-run.
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
    """Replay-stability: the id is a function of the execution, never of when it is computed.

    A child id built from anything the workflow cannot re-derive from history — a fresh uuid, a
    wall clock — would name a *different* child on replay, which is the non-determinism the SDK
    fails a run for. `run_id` is read from the same history a replay replays, so it is the one
    discriminator that is both stable within a run and different across runs.
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
    """The template path's extra failure: a *successful* prior step blocked the whole retry.

    `TemplateWorkflow` re-runs its steps from the beginning, so on the second execution step 1 —
    which succeeded the first time and closed its child — was refused under `REJECT_DUPLICATE`, and
    the run never reached the step that actually failed. There is no partial-progress mechanism
    here to fall back on: the sequencer's whole contract is "walk the steps in order".
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
    """The run discriminator must not swallow the step id and collapse a run's steps onto one child.

    Written because the obvious wrong fix — replacing the step id with the run id rather than
    adding to it — passes every assertion above while making a two-`job`-step template start its
    second step under the first one's id.
    """
    first = _template_job_step_child(monkeypatch, "run-a", "screen")
    second = _template_job_step_child(monkeypatch, "run-a", "confirm")
    assert first["id"] != second["id"]
    assert first["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
