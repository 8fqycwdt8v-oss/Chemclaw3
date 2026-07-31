"""The `job` step: resolved outside workflow code, and able to fail (REV-13, D-140).

`JobStep` is the one template step kind no test had ever constructed, and it carried two defects
that only a `job` step could reach.

**The resolution read the filesystem from workflow code.** `TemplateWorkflow._run_job_step` called
`chemclaw.connectors.registry.find_job` inside `workflow.unsafe.imports_passed_through()`, so the
connector,
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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio import workflow

from chemclaw.agent.authz import AuthorizationError
from chemclaw.connectors.registry import ConnectorError, discovered, enabled
from chemclaw.core.config import settings
from chemclaw.durable.registry import registered_activities
from chemclaw.durable.template_activities import (
    JobStepInput,
    ResolvedJob,
    StepIdentity,
    authorize_job_step,
)
from chemclaw.durable.template_job import TemplateWorkflow

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connectors"


@pytest.fixture
def fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the registry at the test bundle and return its one job name.

    The shipped bundles all declare their arguments by `params_model` reference, so none of them
    can be launched from a dict this test could write by hand — and the step now *validates* before
    resolving (D-158), which is the behaviour under test rather than an obstacle to it. The fixture
    bundle exists precisely so the durable path can be exercised without inventing a production
    capability, and its one job takes a single declared string.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(_FIXTURE_DIR))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    discovered.cache_clear()
    (manifest,) = enabled()
    (job,) = manifest.jobs
    yield job.name
    discovered.cache_clear()


def _step(job: str, **arguments: object) -> JobStepInput:
    """A `job` step input carrying an ordinary requester identity."""
    return JobStepInput(
        job=job,
        arguments=dict(arguments),
        identity=StepIdentity(actor="chemist-1", roles=[], correlation_id="template-run-1"),
    )


def test_a_declared_job_resolves_to_its_connector_and_queue(fixture_bundle: str) -> None:
    """The four facts a child-workflow start needs, produced outside the workflow."""
    resolved = asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))
    assert isinstance(resolved, ResolvedJob)
    assert (resolved.connector, resolved.job) == ("fixture", fixture_bundle)
    # A queue and a workflow type are what the start actually needs; empty ones would start a child
    # nothing polls, which is the same hang by another route.
    assert resolved.workflow and resolved.task_queue
    # And the *validated* payload, so the workflow cannot start a child with the raw arguments.
    assert resolved.payload == {"subject": "benzene"}


def test_an_unknown_job_fails_the_activity_naming_what_is_declared() -> None:
    """The error a template author needs, raised where Temporal can turn it into a failure.

    `ConnectorError` is a `ValueError`, and `BAD_DATA_RETRY` lists `ValueError` as non-retryable —
    so across an activity boundary this fails on the first attempt instead of being retried five
    times identically. In workflow code the same exception was retried forever.
    """
    with pytest.raises(ConnectorError) as caught:
        asyncio.run(authorize_job_step(_step("no_such_job_anywhere")))
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
    assert "authorize_job_step" in names


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
    edit that re-imports `chemclaw.connectors.registry` here would restore the disk read without
    any test
    noticing — the sequencer's own tests replace the activities and never touch a `job` step.

    Checked against the module source rather than `sys.modules`, because the activity module
    legitimately imports the registry and both are loaded by the time any test runs.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable" / "template_job.py"
    ).read_text()
    assert "chemclaw.connectors.registry" not in source, (
        "durable/template_job.py reaches the connector registry again; the lookup belongs in "
        "authorize_job_step so its answer is recorded in history (REV-13)"
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

    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
    from chemclaw.templates.manifest import Template
    from tests.temporal_env import pydantic_client, start_env_or_skip

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
                activities=[authorize_job_step],
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


# --- DARK-2: the step is authorized and audited as its requester (D-158) -----------------------


def _record_audit(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture the audit events the step emits, instead of writing them to a database."""
    events: list[Any] = []

    class _Sink:
        async def record(self, event: Any) -> None:
            events.append(event)

    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: _Sink())
    return events


_EXPENSIVE_BUNDLE = """\
name: costly
description: a bundle whose one job is declared expensive
jobs:
  - name: run_costly_job
    workflow: CostlyWorkflow
    summary: Run the costly job.
    expensive: true
    precondition: tests.test_template_job_step:refuse_benzene
    params:
      - {name: subject, type: string, description: What to run on.}
"""


class _PreconditionRefused(ValueError):
    """What a declared precondition raises to refuse a launch."""


def refuse_benzene(spec: Any) -> None:
    """A declared precondition, resolved by dotted reference exactly as a real one is."""
    if getattr(spec, "subject", None) == "benzene":
        raise _PreconditionRefused("this job refuses benzene")


@pytest.fixture
def costly_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    """A discovered bundle whose one job is `expensive` and carries a `precondition`.

    Written to disk and read back through the real registry rather than hand-built, because the
    two fields under test are exactly the ones `ResolvedJob` used to drop between the manifest and
    the launch — a hand-constructed `JobSpec` would prove nothing about that journey.
    """
    bundle = tmp_path / "costly"
    bundle.mkdir()
    (bundle / "connector.yaml").write_text(_EXPENSIVE_BUNDLE, encoding="utf-8")
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(tmp_path))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    discovered.cache_clear()
    yield "run_costly_job"
    discovered.cache_clear()


def test_an_expensive_job_step_is_refused_for_an_unentitled_requester(
    monkeypatch: pytest.MonkeyPatch, costly_bundle: str
) -> None:
    """The finding: a template was a way to start HPC work you could not start yourself.

    `ResolvedJob` dropped `expensive`, so `authorize_trigger` never ran on this path and a template
    naming `compute_dft_energy` started it for anyone entitled to run the *template*. The check now
    happens against the step's own requester, before any child workflow is started.
    """
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", costly_bundle)
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")

    with pytest.raises(AuthorizationError) as caught:
        asyncio.run(authorize_job_step(_step(costly_bundle, subject="toluene")))
    assert "chemist-1" in str(caught.value)


def test_an_entitled_requester_passes_the_same_gate(
    monkeypatch: pytest.MonkeyPatch, costly_bundle: str
) -> None:
    """The other half: the gate is a gate, not a wall — holding the role gets through it."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", costly_bundle)
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")

    step = JobStepInput(
        job=costly_bundle,
        arguments={"subject": "toluene"},
        identity=StepIdentity(actor="chemist-1", roles=["compute"], correlation_id="run-1"),
    )
    assert asyncio.run(authorize_job_step(step)).payload == {"subject": "toluene"}


def test_a_declared_precondition_runs_on_the_template_path_too(costly_bundle: str) -> None:
    """`ResolvedJob` dropped `precondition` as well, and it has no other replay-safe home.

    `JobSpec.precondition` documents the launch boundary as the only place such a guard can live —
    a pydantic validator or a check inside the workflow re-runs on replay against *current* config.
    The template path had no launch boundary that ran it, so a job's own domain rule simply did not
    apply to any template that used it.
    """
    with pytest.raises(_PreconditionRefused):
        asyncio.run(authorize_job_step(_step(costly_bundle, subject="benzene")))


def test_the_launch_leaves_a_gxp_audit_row_naming_the_requester(
    monkeypatch: pytest.MonkeyPatch, fixture_bundle: str
) -> None:
    """A durable launch from a template used to leave no audit record at all.

    The row has to name the job (so it reads like the same launch from a chat turn), the person who
    asked, and the run that tied the steps together — otherwise the GxP question "who started this
    calculation" has no answer for anything a template did.
    """
    events = _record_audit(monkeypatch)
    asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))
    (event,) = events
    assert (event.tool, event.actor, event.outcome) == (fixture_bundle, "chemist-1", "ok")
    assert event.correlation_id == "template-run-1"


def test_a_refused_launch_is_audited_as_an_error_before_it_raises(
    monkeypatch: pytest.MonkeyPatch, costly_bundle: str
) -> None:
    """A refusal is the row an auditor most wants; it must not be the one that goes missing."""
    events = _record_audit(monkeypatch)
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_expensive_actions", costly_bundle)
    monkeypatch.setattr(settings, "entra_privileged_roles", "compute")

    with pytest.raises(AuthorizationError):
        asyncio.run(authorize_job_step(_step(costly_bundle, subject="toluene")))
    (event,) = events
    assert (event.tool, event.actor, event.outcome) == (costly_bundle, "chemist-1", "error")


def test_a_step_with_bad_arguments_fails_before_any_workflow_starts(fixture_bundle: str) -> None:
    """Validation moved onto this path too: the child used to be started with whatever was written.

    `_run_job_step` passed `resolve(step.arguments, scope)` straight into the child's payload, so a
    template with a misspelled argument produced a durable run that failed somewhere inside the
    connector's workflow rather than a step that refused to start.
    """
    with pytest.raises(ValidationError):
        asyncio.run(authorize_job_step(_step(fixture_bundle, subjekt="benzene")))
