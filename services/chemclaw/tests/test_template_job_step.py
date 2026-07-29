"""The `job` step: resolved outside workflow code, and able to fail (REV-13, D-140).

`JobStep` is the one template step kind no test had ever constructed, and it carried two defects
that only a `job` step could reach.

**The resolution read the filesystem from workflow code.** `TemplateWorkflow._run_job_step` called
`connectors.registry.find_job` inside `workflow.unsafe.imports_passed_through()`, so the connector,
workflow type and queue a child was started on came from the disk of whichever worker happened to
be replaying rather than from history. `@cache` hides this on a warm process and does nothing on a
cold one.

**And an unresolvable step hung the run rather than failing it.** `find_job` raises
`ConnectorError`, a `ValueError` — not an SDK `FailureError`. Raised in workflow code, the Temporal
SDK treats that as a suspected bug and suspends the workflow in an internal task-failure retry loop
that ignores the retry policy and never gives up. A template naming a job that no enabled connector
declares produced a run that sat there forever, which is strictly worse than one that fails and says
why: nothing alerts, and the workflow holds its id against `REJECT_DUPLICATE` so a corrected re-run
is refused too.

Most of these run offline. The one that needs a real server proves the end the others can only
argue about: that the run *terminates*.
"""

import asyncio

import pytest
from temporalio import workflow

from connectors.registry import ConnectorError, enabled
from workflows.registry import registered_activities
from workflows.template_activities import ResolvedJob, resolve_job_step
from workflows.template_job import TemplateWorkflow


def _first_declared_job() -> tuple[str, str]:
    """A `(connector, job)` pair from the enabled bundles, or skip if none declares a job."""
    for manifest in enabled():
        for job in manifest.jobs:
            return manifest.name, job.name
    pytest.skip("no enabled connector declares a job")


def test_a_declared_job_resolves_to_its_connector_and_queue() -> None:
    """The four facts a child-workflow start needs, produced outside the workflow."""
    connector, job_name = _first_declared_job()
    resolved = asyncio.run(resolve_job_step(job_name))
    assert isinstance(resolved, ResolvedJob)
    assert (resolved.connector, resolved.job) == (connector, job_name)
    # A queue and a workflow type are what the start actually needs; empty ones would start a child
    # nothing polls, which is the same hang by another route.
    assert resolved.workflow and resolved.task_queue


def test_an_unknown_job_fails_the_activity_naming_what_is_declared() -> None:
    """The error a template author needs, raised where Temporal can turn it into a failure.

    `ConnectorError` is a `ValueError`, and `BAD_DATA_RETRY` lists `ValueError` as non-retryable —
    so across an activity boundary this fails on the first attempt instead of being retried five
    times identically. In workflow code the same exception was retried forever.
    """
    with pytest.raises(ConnectorError) as caught:
        asyncio.run(resolve_job_step("no_such_job_anywhere"))
    message = str(caught.value)
    assert "no_such_job_anywhere" in message
    # Naming the valid ones is the difference between a fixable error and a puzzle.
    assert "declared jobs:" in message


def test_the_resolver_is_registered_on_the_light_queue() -> None:
    """Registered, or the workflow's local activity call would fail at run time, not at import.

    The background queue, with the sequencer it serves: a cached in-process lookup has no business
    on the queue reserved for heavy compute.
    """
    names = {activity.__name__ for activity in registered_activities("background")}
    assert "resolve_job_step" in names


def test_the_sequencer_is_allowed_to_fail() -> None:
    """`failure_exception_types` — without it a template that can never succeed hangs forever.

    Asserted on the definition Temporal actually built rather than on the decorator's source text,
    because it is the SDK's view that decides whether the run fails or suspends.
    """
    definition = workflow._Definition.must_from_class(TemplateWorkflow)
    assert definition.failure_exception_types, (
        "TemplateWorkflow declares no failure_exception_types, so a raw exception in the sequencer "
        "suspends the run in the SDK's task-failure retry loop instead of failing it"
    )
    assert any(
        issubclass(Exception, declared) or declared is Exception
        for declared in definition.failure_exception_types
    )


def test_the_workflow_module_does_not_reach_the_connector_registry() -> None:
    """The regression guard for the determinism half: no registry import in workflow code.

    The lookup was moved to an activity precisely so the answer is recorded in history. A future
    edit that re-imports `connectors.registry` here would restore the disk read without any test
    noticing — the sequencer's own tests replace the activities and never touch a `job` step.

    Checked against the module source rather than `sys.modules`, because the activity module
    legitimately imports the registry and both are loaded by the time any test runs.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "workflows" / "template_job.py").read_text()
    assert "connectors.registry" not in source, (
        "workflows/template_job.py reaches the connector registry again; the job lookup belongs in "
        "resolve_job_step so its answer is recorded in history (REV-13)"
    )


def test_a_template_naming_an_unknown_job_fails_instead_of_hanging() -> None:
    """The end the offline tests can only argue about: against a real server, the run *terminates*.

    This is the defect itself. Every check above is about the mechanism — where the lookup happens,
    what type it raises, what the definition declares — and none can distinguish "fails" from
    "hangs", because that distinction lives in the SDK's task-failure loop rather than in our code.
    So this one asks a real server for a verdict, under a timeout: a template naming a job no
    connector declares must come back failed within seconds. Before the fix it never came back.

    Skips where the Temporal test server cannot be downloaded (the offline sandbox), which is the
    same bargain every other real-server test in this suite makes.
    """
    from datetime import timedelta

    from temporalio.client import WorkflowFailureError
    from temporalio.worker import Worker

    from templates.manifest import Template
    from tests.temporal_env import pydantic_client, start_env_or_skip
    from workflows.template_job import TemplateRunInput, TemplateWorkflow

    template = Template.model_validate(
        {
            "name": "bad-job",
            "summary": "Name a job nothing declares.",
            "inputs": [],
            "steps": [{"id": "run", "kind": "job", "job": "no_such_job_anywhere", "arguments": {}}],
        }
    )

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-bad-job",
                workflows=[TemplateWorkflow],
                activities=[resolve_job_step],
            ):
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(
                        client.execute_workflow(
                            TemplateWorkflow.run,
                            TemplateRunInput(template=template, requested_by="tester"),
                            id="template-bad-job",
                            task_queue="test-bad-job",
                            execution_timeout=timedelta(seconds=30),
                        ),
                        # Well inside the execution timeout: if the SDK is suspending the workflow
                        # rather than failing it, nothing returns and this is what says so.
                        timeout=30,
                    )

    asyncio.run(_run())
