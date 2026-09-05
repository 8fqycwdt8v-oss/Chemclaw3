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

import ast
import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from temporalio import activity, workflow

from chemclaw.agent.authz import AuthorizationError
from chemclaw.connectors.manifest import JobSpec
from chemclaw.connectors.registry import ConnectorError, enabled, find_job
from chemclaw.core.config import _WRAPPER_FINISH_STEPS, settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.logging import ContextFilter
from chemclaw.core.session_context import get_current_session_id
from chemclaw.durable import template_activities
from chemclaw.durable.connector_job import (
    _FINISH_STEPS,
    ConnectorJobInput,
    child_execution_timeout,
    wrapper_execution_timeout,
)
from chemclaw.durable.registry import registered_activities
from chemclaw.durable.template_activities import (
    JobStepInput,
    ResolvedJob,
    StepIdentity,
    _acting_as,
    authorize_job_step,
)
from chemclaw.durable.template_job import TemplateWorkflow

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "connectors"


@pytest.fixture
def fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the registry at the test bundle and return its one job name.

    The shipped bundles all declare their arguments by `params_model` reference, so none of them
    can be launched from a dict this test could write by hand — and the step now *validates* before
    resolving (D-168), which is the behaviour under test rather than an obstacle to it. The fixture
    bundle exists precisely so the durable path can be exercised without inventing a production
    capability, and its one job takes a single declared string.

    Discovery is cached, but `tests/conftest.py`'s autouse fixture clears it around every test, so
    repointing `connectors_dir` here needs no local `cache_clear()`.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(_FIXTURE_DIR))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    (manifest,) = enabled()
    (job,) = manifest.jobs
    yield job.name


def _step(job: str, **arguments: object) -> JobStepInput:
    """A `job` step input carrying an ordinary requester identity."""
    return JobStepInput(
        job=job,
        arguments=dict(arguments),
        identity=StepIdentity(actor="chemist-1", roles=[], correlation_id="template-run-1"),
    )


def test_a_step_runs_under_the_correlation_id_its_run_was_launched_with() -> None:
    """The third ambient `_acting_as` dropped, read through the consumers that actually read it.

    `StepIdentity.correlation_id` is `min_length=1` and its comment says it ties the run's audit
    events together; nothing stamped it, so every consumer of the *ambient* id saw none. The two
    asserted here are the ones that hurt: the three ambient getters, which `connectors/jobs.py`
    reads for the id it hands a launched job, and `ContextFilter`, which puts the id on a log
    line — it writes `"-"` when there is
    none, which is why a paged engineer looking at a running durable job had nothing to grep back to
    the turn behind it. The audit trail is deliberately *not* asserted: `agent/audit.py` falls back
    to the id each step activity passes it explicitly, so its rows were right all along and would
    pass this test with the stamp removed.

    The teardown half is asserted too, because a bracket that leaks leaks one run's identity into
    whatever the worker picks up next.
    """
    identity = StepIdentity(
        actor="chemist-1", roles=[], correlation_id="template-run-1", session_id="s-tmpl"
    )
    context = ContextFilter()

    def _ambient() -> tuple[str, str, str]:
        """The three ambient values, read the way their consumers read them.

        Read here rather than through a helper: `kg/proposal.ambient_provenance` used to bundle
        them for the PR-gate's record, and went with it
        (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). The invariant this test holds is
        about the *stamp*, not about that wrapper.
        """
        return (
            get_current_actor() or "",
            get_current_session_id() or "",
            get_current_correlation_id() or "",
        )

    def _stamped() -> str:
        """The correlation id `ContextFilter` puts on a *fresh* record right now.

        A fresh record per reading, because the filter stamps with `setdefault` rather than
        assignment (`core/logging.py`) — so re-filtering the record from inside the bracket would
        read the id it already carries and would pass however the bracket behaved.
        """
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "still running", None, None)
        context.filter(record)
        # Defaulted because the attribute is stamped by the filter, not declared on the
        # record — an unstamped record reads `""` here and fails, which is the point.
        return str(getattr(record, "correlation_id", ""))

    with _acting_as(identity):
        assert _ambient() == ("chemist-1", "s-tmpl", "template-run-1")
        assert _stamped() == "template-run-1"

    assert _ambient() == ("", "", "")
    assert _stamped() == "-"


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


def _template_path_job_input_fields() -> set[str]:
    """Which `ConnectorJobInput` fields `TemplateWorkflow`'s literal actually names.

    Read off the AST rather than by substring, because this module argues for its fields in prose
    beside them: a comment naming a field it forgot to pass would satisfy a `in source` check,
    which is precisely the failure being guarded.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable" / "template_job.py"
    ).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ConnectorJobInput":
            return {keyword.arg for keyword in node.keywords if keyword.arg}
    raise AssertionError("durable/template_job.py no longer builds a ConnectorJobInput literal")


def test_every_manifest_field_the_job_wrapper_reads_survives_the_template_path(
    fixture_bundle: str,
) -> None:
    """A field the template path drops is a field that silently means something else on it.

    Three have now gone missing this way. `session_id` and `correlation_id` went first, and the
    comment left behind said in as many words that this is the shape to watch for. `awaits_answer`
    then went the same way one merge later: `ResolvedJob` did not declare it, so a template `job`
    step handed `ConnectorJobInput` the default and the child got the five-hour fleet ceiling —
    measured, `direct awaits_answer=True child execution_timeout=None` against `template
    awaits_answer=False child execution_timeout=5:00:00` for the same manifest, on the one job
    whose own wait is fourteen days.

    The set is **derived**, not listed: what a manifest declares (`JobSpec`) intersected with what
    the wrapper reads (`ConnectorJobInput`) is exactly the set that has to survive resolution, so a
    sixth such field is in this check the day it is declared rather than the day someone remembers
    to add it here. Both halves of the path are asserted, because the two failures are independent
    — a field can be missing from `ResolvedJob`, or present there and not passed on.
    """
    declared = set(JobSpec.model_fields) & set(ConnectorJobInput.model_fields)
    assert declared, "the intersection is empty; this test has stopped asking anything"
    missing = declared - set(ResolvedJob.model_fields)
    assert not missing, (
        f"{sorted(missing)} is declared on a manifest and read by ConnectorJobInput but is not "
        "carried by ResolvedJob, so the template path silently substitutes its default"
    )
    not_passed = declared - _template_path_job_input_fields()
    assert not not_passed, (
        f"TemplateWorkflow builds its ConnectorJobInput without {sorted(not_passed)}, so a job "
        "launched from a template is configured differently from the same job launched from chat"
    )
    # And the resolver fills them from the manifest rather than leaving the model's defaults.
    _connector, job = find_job(fixture_bundle)
    resolved = asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))
    assert {field: getattr(resolved, field) for field in declared} == {
        field: getattr(job, field) for field in declared
    }


def test_a_job_that_waits_on_a_person_is_unbounded_as_a_template_step_too(
    monkeypatch: pytest.MonkeyPatch, fixture_bundle: str
) -> None:
    """The child ceiling the dropped field decided, asserted on the number rather than the wiring.

    `awaits_answer` exists because wall clock is not cost for a job that suspends on a plate:
    `child_execution_timeout` hands such a job no execution timeout at all, since the shipped
    campaign opens waits totalling 154 days under a five-hour ceiling. That reasoning applied only
    to the chat launcher for as long as `ResolvedJob` did not carry the field.

    The fixture bundle's job does not declare it — no in-tree fixture does — so the declaration is
    substituted at `find_job`, which is where the manifest enters this activity. That keeps the
    subject the *resolution*: everything after the substitution is the shipped path.
    """
    connector, job = find_job(fixture_bundle)
    waiting = job.model_copy(update={"awaits_answer": True})
    monkeypatch.setattr(template_activities, "find_job", lambda _name: (connector, waiting))
    resolved = asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))
    assert resolved.awaits_answer is True
    assert child_execution_timeout(resolved.timeout_seconds, resolved.awaits_answer) is None, (
        "a job that suspends on a durable answer was handed a wall-clock ceiling because it "
        "reached the child through a template step instead of a chat turn"
    )


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
                task_queue=settings.background_task_queue,
                workflows=[TemplateWorkflow],
                activities=[authorize_job_step, _swallow_record],
            ):
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(
                        client.execute_workflow(
                            TemplateWorkflow.run,
                            TemplateRunInput(template=template, requested_by="tester"),
                            id="template-bad-job",
                            task_queue=settings.background_task_queue,
                            execution_timeout=timedelta(seconds=30),
                        ),
                        # Well inside the execution timeout: if the SDK is suspending the workflow
                        # rather than failing it, nothing returns and this is what says so.
                        timeout=30,
                    )

    asyncio.run(_run())


# --- DARK-2: the step is authorized and audited as its requester (D-168) -----------------------


# `TemplateWorkflow` records a `job_records` row on both its paths
# (`D-2026-09-05-a-procedure-that-leaves-no-record`), so a worker that runs the workflow must serve
# the activity or the run waits on it. These tests are about step behaviour rather than about
# recording, so they serve a no-op: `tests/test_template_job_record.py` owns the recording contract.
@activity.defn(name="record_job")
async def _swallow_record(record: Any) -> None:
    """Accept the run's durable record and discard it — this file is not about that write."""


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

    Discovery is cached, but `tests/conftest.py`'s autouse fixture clears it around every test, so
    repointing `connectors_dir` here needs no local `cache_clear()`.
    """
    bundle = tmp_path / "costly"
    bundle.mkdir()
    (bundle / "connector.yaml").write_text(_EXPENSIVE_BUNDLE, encoding="utf-8")
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(tmp_path))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")
    yield "run_costly_job"


def test_an_expensive_job_step_is_refused_for_an_unentitled_requester(
    monkeypatch: pytest.MonkeyPatch, costly_bundle: str
) -> None:
    """The finding: a template was a way to start expensive work you could not start yourself.

    `ResolvedJob` dropped `expensive`, so `authorize_trigger` never ran on this path and a template
    naming `sample_conformers` started it for anyone entitled to run the *template*. The check now
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


def test_the_launch_leaves_an_audit_row_naming_the_requester(
    monkeypatch: pytest.MonkeyPatch, fixture_bundle: str
) -> None:
    """A durable launch from a template used to leave no audit record at all.

    The row has to name the job (so it reads like the same launch from a chat turn), the person who
    asked, and the run that tied the steps together — otherwise the question "who started this
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
    assert (event.tool, event.actor, event.outcome) == (costly_bundle, "chemist-1", "refused")


def test_a_step_with_bad_arguments_fails_before_any_workflow_starts(fixture_bundle: str) -> None:
    """Validation moved onto this path too: the child used to be started with whatever was written.

    `_run_job_step` passed `resolve(step.arguments, scope)` straight into the child's payload, so a
    template with a misspelled argument produced a durable run that failed somewhere inside the
    connector's workflow rather than a step that refused to start.
    """
    with pytest.raises(ValidationError):
        asyncio.run(authorize_job_step(_step(fixture_bundle, subjekt="benzene")))


def test_every_template_step_activity_is_registered_on_a_worker() -> None:
    """A template's `tool` and `agent` steps were served by no worker at all.

    Only the job-step resolver carried `@durable_activity`; `run_tool_step` and `run_agent_step`
    had a bare `@activity.defn`, so nothing registered them and the shipped `hazard-briefing`
    template failed on its *first* step against a real server with "Activity function
    run_tool_step ... is not registered on this worker". Found by running it live for D-168.

    Asserted over all three together rather than one at a time, because the failure mode is a new
    step kind arriving without its registration — which is exactly what happened here, twice.
    """
    names = {activity.__name__ for activity in registered_activities("background")}
    assert {"authorize_job_step", "run_tool_step", "run_agent_step"} <= names, (
        f"template step activities missing from the background worker: "
        f"{sorted({'authorize_job_step', 'run_tool_step', 'run_agent_step'} - names)}"
    )


def test_the_run_is_started_with_a_whole_procedure_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A template run had no execution timeout, so its only bound was step budget × step count.

    That product is a number nothing declares: it grows silently every time an author adds a step,
    no operator can read it off any setting, and a wedged procedure — as opposed to a wedged
    *step* — had nothing to stop it. `ConnectorJobWorkflow` gives the children it starts
    `connector_job_timeout_seconds` for precisely this reason (`durable/connector_job.py`), and a
    template is core's own sequencer of the same kind of work.

    Asserted at the launch rather than on the setting, because the setting existing is not the fix:
    the defect was a `start_workflow` call that never passed one.
    """
    from datetime import timedelta

    from chemclaw.templates.manifest import Template
    from chemclaw.templates.registry import build_template_tool

    started: list[dict[str, Any]] = []

    class _FakeClient:
        async def start_workflow(self, _run: Any, arg: Any, **kwargs: Any) -> Any:
            started.append({"input": arg, **kwargs})
            return type("Handle", (), {"id": kwargs["id"]})()

    async def connect() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr("chemclaw.templates.registry.connect", connect)
    monkeypatch.setattr("chemclaw.templates.registry.require_actor", lambda: "chemist@lab")

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Do the thing.",
            "inputs": [],
            "steps": [{"id": "brief", "kind": "agent", "prompt": "write it up"}],
        }
    )
    asyncio.run(build_template_tool(template)(params={}))

    (call,) = started
    assert call["execution_timeout"] == timedelta(seconds=settings.template_run_timeout_seconds), (
        "TemplateWorkflow is started with no run-level ceiling, so an N-step template's only "
        "bound is template_step_timeout_seconds x N"
    )


def test_the_run_ceiling_must_be_able_to_contain_one_step() -> None:
    """A run ceiling at or below the step budget kills the procedure inside its own first step.

    And it does so with a bare `WorkflowExecutionTimedOut` naming neither setting, while making the
    per-step timeout that was *meant* to fire unreachable. Refused by the config rather than
    discovered in production — the same rule `_the_fan_out_ceiling_covers_the_section_it_bounds`
    already states for the other parent/child pair that has a ceiling.
    """
    from chemclaw.core.config import Settings

    with pytest.raises(ValidationError) as caught:
        Settings(template_step_timeout_seconds=900.0, template_run_timeout_seconds=900.0)
    assert "template_run_timeout_seconds" in str(caught.value)


def test_the_run_ceiling_must_be_able_to_contain_one_job_step() -> None:
    """The same rule against the bound a `job` step actually carries, which is not the step budget.

    `template_step_timeout_seconds` bounds an `agent` or a `tool` step. A `job` step is bounded by
    `wrapper_execution_timeout()` — `connector_job_timeout_seconds` plus the four post-child steps
    the wrapper still owes — which shipped at 18,120 s inside a run ceiling of 7,200 s, so one
    legitimate CREST search ended the whole procedure as a bare `TIMED_OUT`: an execution timeout is
    not delivered to workflow code, so `TemplateWorkflow`'s `except BaseException ->
    _notify_failure` never ran, the chemist got nothing on the session stream, and the connector
    child was terminated with its parent before it could write its own failure row. The validator
    that exists for this relation was checking the one number that does not bound a `job` step.

    Two halves, and both are needed. The pair must be *refused* when inverted — otherwise the
    default is the only thing standing between a deployment and a silent run — and the **shipped**
    defaults must clear the bound, because a validator whose own defaults violate it refuses every
    process at import.
    """
    from chemclaw.core.config import Settings

    with pytest.raises(ValidationError) as caught:
        Settings(template_run_timeout_seconds=7200.0)
    message = str(caught.value)
    assert "template_run_timeout_seconds" in message
    assert "connector_job_timeout_seconds" in message

    assert settings.template_run_timeout_seconds > wrapper_execution_timeout().total_seconds(), (
        "the shipped run ceiling cannot contain one job step, so seven of the nine shipped "
        "templates can end as a silent TIMED_OUT"
    )


def test_the_configs_restatement_of_the_wrapper_ceiling_cannot_drift() -> None:
    """`core` may not import `durable`, so the config restates `_FINISH_STEPS`. This pins the pair.

    `tests/test_layering.py` enforces that `chemclaw.core` imports no sibling, so the validator
    above cannot call `wrapper_execution_timeout()` and has to spell its arithmetic out again. A
    restatement nothing checks is the duplication moved rather than removed: add a fifth
    post-child step to `ConnectorJobWorkflow` and the validator would go on clearing a bound that
    is 30 s short, which is exactly the silent inversion it was written to end.

    Asserted twice on purpose. The constants must agree — that is the readable failure — and the
    whole identity must hold, so that a change to the *shape* of `wrapper_execution_timeout` (a new
    term, a different budget) is caught too and not just a change to its count.
    """
    assert _WRAPPER_FINISH_STEPS == _FINISH_STEPS, (
        "core/config restates durable/connector_job.py::_FINISH_STEPS because it may not import it"
    )
    assert wrapper_execution_timeout().total_seconds() == (
        settings.connector_job_timeout_seconds
        + settings.activity_timeout_seconds * _WRAPPER_FINISH_STEPS
    )


def test_a_failed_template_step_wakes_the_session_and_names_which_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template that dies at step 2 of 3 must reach the chemist, and say where it died.

    The exact defect `connector_job` had already been fixed for, one workflow over and never
    carried across: `TemplateWorkflow.run` reached its `job_completed` push-back only on the success
    path and had no `except` at all, so a failed run ended in silence. The chemist who launched a
    procedure was told it had started and then never told anything else; the reason existed only in
    Temporal's history under an id nobody had kept.

    Three assertions, and the third is the one with teeth. That an event fires is easy to satisfy
    trivially. That the *step id* is on it is what makes the event worth delivering — "the template
    failed" is unactionable for a procedure with several steps, and it is the one thing this
    workflow knows that the failure itself does not. And the run must still fail: a push-back that
    swallowed the exception would turn a broken procedure into a silently empty result, which is a
    worse defect than the one being fixed.

    Driven against a real Temporal server rather than by calling `run` directly, because the thing
    under test is behaviour on the *failure* path of a workflow — where the SDK's own handling of an
    exception raised in workflow code is exactly what `failure_exception_types` above had to be
    added for. Skips where the test server cannot be downloaded, like every real-server test here.
    """
    import inspect
    from datetime import timedelta

    from temporalio.client import WorkflowFailureError
    from temporalio.worker import Worker

    from chemclaw.agent.session_events import record_session_event
    from chemclaw.durable.notify import record_session_event_activity
    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
    from chemclaw.templates.manifest import Template
    from tests.temporal_env import pydantic_client, start_env_or_skip

    _QUEUE = "background-jobs"
    notified: list[tuple[str, str, dict[str, Any]]] = []

    async def _fake_record(*args: Any, **kwargs: Any) -> None:
        bound = inspect.signature(record_session_event).bind(*args, **kwargs)
        notified.append(
            (bound.arguments["session_id"], bound.arguments["kind"], bound.arguments["payload"])
        )

    monkeypatch.setattr("chemclaw.durable.notify.record_session_event", _fake_record)
    # The push-back runs on the background queue by name, so the one worker here has to *be* that
    # queue — otherwise the activity is scheduled to a queue nobody polls and the run hangs until
    # its execution timeout, which is a passing-looking 30-second failure rather than a defect.
    monkeypatch.setattr("chemclaw.core.config.settings.background_task_queue", _QUEUE)

    # The step names a job no connector declares, which is the same reachable, deterministic
    # failure `test_a_template_naming_an_unknown_job_fails_rather_than_hanging` uses. The first step
    # is there so the failure is genuinely mid-procedure rather than at the very first thing tried.
    template = Template.model_validate(
        {
            "name": "fails-midway",
            "summary": "Fail on the one step it has.",
            "inputs": [],
            "steps": [
                {"id": "first", "kind": "job", "job": "no_such_job_anywhere", "arguments": {}},
            ],
        }
    )

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=_QUEUE,
                workflows=[TemplateWorkflow],
                activities=[authorize_job_step, record_session_event_activity, _swallow_record],
            ):
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(
                        client.execute_workflow(
                            TemplateWorkflow.run,
                            TemplateRunInput(
                                template=template, requested_by="tester", session_id="s-tmpl"
                            ),
                            id="template-failure-notify",
                            task_queue=_QUEUE,
                            execution_timeout=timedelta(seconds=30),
                        ),
                        timeout=30,
                    )

    asyncio.run(_run())

    assert len(notified) == 1, "a failed template emitted no session event at all — the defect"
    session_id, kind, payload = notified[0]
    assert (session_id, kind) == ("s-tmpl", "job_failed")
    assert payload["step"] == "first", (
        "which step failed is the one thing this workflow knows that the failure does not"
    )
    assert payload["template"] == "fails-midway"


def test_a_declared_optional_input_the_caller_omitted_resolves_to_none() -> None:
    """Omitting an optional argument killed every template on its first step, at run time.

    `registry.py` dumps the launch params with `exclude_none=True`, so an optional input the caller
    left out was simply absent from `run.inputs` — and every template in the tree references its
    optional `solvent` unconditionally (`solvent: "${inputs.solvent}"`). The result was

        UnresolvedReference: template references 'inputs.solvent', which is not available;
                             have: ['inputs.smiles']

    on step 1, after the launch, inside the workflow. `conformer-refinement.yaml` has had it since
    the day it shipped, so "run this in the gas phase" — the omitted-solvent default, and the
    commonest call there is — had never worked for any template.

    Driven through a real workflow rather than asserted on the scope dict, because the scope is
    built inside `TemplateWorkflow.run` and the failure was in what the *launcher* handed it. The
    template here is the exact shape the shipped ones use: one required input, one optional one, and
    an argument that references the optional one whole.
    """
    from datetime import timedelta

    from temporalio.worker import Worker

    from chemclaw.durable.template_activities import AgentStepInput
    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
    from chemclaw.templates.manifest import Template
    from chemclaw.templates.resolve import resolve
    from tests.temporal_env import pydantic_client, start_env_or_skip

    template = Template.model_validate(
        {
            "name": "optional-input",
            "summary": "Reference an optional input the caller did not give.",
            "inputs": [
                {"name": "smiles", "type": "string", "description": "The molecule."},
                {
                    "name": "solvent",
                    "type": "string",
                    "description": "Implicit solvent; omitted for gas phase.",
                    "required": False,
                },
            ],
            "steps": [
                {
                    "id": "note",
                    "kind": "agent",
                    "purpose": "Echo what resolved.",
                    "prompt": "solvent=${inputs.solvent} smiles=${inputs.smiles}",
                }
            ],
        }
    )

    seen: list[str] = []

    @activity.defn(name="run_agent_step")
    async def _agent(step: AgentStepInput) -> str:
        seen.append(step.prompt)
        return "ok"

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[TemplateWorkflow],
                activities=[_agent, _swallow_record],
            ):
                await asyncio.wait_for(
                    client.execute_workflow(
                        TemplateWorkflow.run,
                        # `solvent` deliberately absent, exactly as `exclude_none` leaves it.
                        TemplateRunInput(
                            template=template,
                            inputs={"smiles": "CCO"},
                            requested_by="tester",
                        ),
                        id="template-optional-input",
                        task_queue=settings.background_task_queue,
                        execution_timeout=timedelta(seconds=30),
                    ),
                    timeout=30,
                )

    asyncio.run(_run())

    # Reaching the step at all is the assertion: before this, the run died here.
    assert seen == ["solvent=null smiles=CCO"], (
        "an omitted optional input must resolve rather than raise; "
        "every shipped template references one unconditionally"
    )

    # And the form the templates actually use — a whole-string reference in `arguments:` — must
    # carry `None` itself rather than the text "null", because that is what the calc specs default
    # to and what `solvents.require_supported_solvents` reads as gas phase. An empty string does
    # *not* work: `unsupported([""])` returns `[""]`, so a literal "" fails the precondition.
    assert resolve("${inputs.solvent}", {"inputs.solvent": None}) is None
