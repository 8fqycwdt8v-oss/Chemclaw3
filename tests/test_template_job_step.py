"""The `job` step: resolved outside workflow code, and able to fail.

The job is resolved in an activity so the connector, workflow type and queue come from history
rather than the replaying worker's disk. An unresolvable job must fail the run: a `ValueError`
raised in workflow code makes the SDK retry the task forever, holding the workflow id against
`REJECT_DUPLICATE`. Most tests run offline; one needs a real server to prove the run terminates.
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
from chemclaw.connectors.jobs import ConnectorJobError
from chemclaw.connectors.manifest import JobSpec
from chemclaw.connectors.registry import ConnectorError, enabled, find_job
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.logging import ContextFilter
from chemclaw.core.session_context import get_current_session_id
from chemclaw.durable import template_activities
from chemclaw.durable.connector_job import (
    ConnectorJobInput,
    child_execution_timeout,
    finish_headroom,
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


@pytest.fixture(autouse=True)
def _allow_test_package_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this suite's fixture bundle name a `precondition:` that lives in this file.

    A precondition is imported and called, so it is held to the package allow-list; this suite sets
    it as an out-of-tree deployment would.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")


@pytest.fixture
def fixture_bundle(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the registry at the test bundle and return its one job name.

    Shipped bundles declare arguments by `params_model`, and the step validates before resolving, so
    a fixture bundle whose job takes one declared string exercises the durable path. The conftest's
    autouse fixture clears the discovery cache around every test.
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
    """A step runs under the correlation id its run was launched with.

    Asserted through the consumers that read the ambient id: the three ambient getters
    (`connectors/jobs.py` passes the id to a launched job) and `ContextFilter`, which otherwise logs
    `"-"`. The audit trail is not asserted because it receives the id explicitly. Teardown is
    asserted too, so one run's identity does not leak into the next.
    """
    identity = StepIdentity(
        actor="chemist-1", roles=[], correlation_id="template-run-1", session_id="s-tmpl"
    )
    context = ContextFilter()

    def _ambient() -> tuple[str, str, str]:
        """The three ambient values, read the way their consumers read them."""
        return (
            get_current_actor() or "",
            get_current_session_id() or "",
            get_current_correlation_id() or "",
        )

    def _stamped() -> str:
        """The correlation id `ContextFilter` puts on a fresh record right now.

        A fresh record each time, because the filter uses `setdefault` and a re-filtered record
        keeps its first id.
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

    Read off the AST, so a comment naming a field cannot satisfy the check.
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
    """Every manifest field the job wrapper reads survives the template path.

    A dropped field silently means something else on that path (e.g. `awaits_answer` defaulting to
    the five-hour ceiling). The set is derived as `JobSpec` ∩ `ConnectorJobInput`, so a new field is
    covered on the day it is declared. Both `ResolvedJob` and what is passed on are asserted.
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
    """A funded job that waits on a person is unbounded as a template step too.

    `child_execution_timeout` gives an `awaits_answer` job no execution timeout, since its waits can
    total months. The declaration is substituted at `find_job` and the operator's grant is given,
    since `require_funded_ceiling` in `prepare_job_launch` refuses an unfunded one on both
    launchers.
    """
    connector, job = find_job(fixture_bundle)
    waiting = job.model_copy(update={"awaits_answer": True})
    monkeypatch.setattr(template_activities, "find_job", lambda _name: (connector, waiting))
    monkeypatch.setattr(settings, "connector_jobs_awaiting_answer", f"{connector}.{waiting.name}")
    resolved = asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))
    assert resolved.awaits_answer is True
    assert child_execution_timeout(resolved.timeout_seconds, resolved.awaits_answer) is None, (
        "a job that suspends on a durable answer was handed a wall-clock ceiling because it "
        "reached the child through a template step instead of a chat turn"
    )


def test_a_template_step_cannot_launch_a_wait_the_operator_never_funded(
    monkeypatch: pytest.MonkeyPatch, fixture_bundle: str
) -> None:
    """A template step cannot launch a wait the operator never funded.

    An `awaits_answer` job has no wall-clock ceiling, so it is refused inside `prepare_job_launch`,
    before the workflow starts and before the bundle's precondition runs, with a message naming the
    setting.
    """
    connector, job = find_job(fixture_bundle)
    waiting = job.model_copy(update={"awaits_answer": True})
    monkeypatch.setattr(template_activities, "find_job", lambda _name: (connector, waiting))
    monkeypatch.setattr(settings, "connector_jobs_awaiting_answer", "")
    with pytest.raises(ConnectorJobError, match="CHEMCLAW_CONNECTOR_JOBS_AWAITING_ANSWER"):
        asyncio.run(authorize_job_step(_step(fixture_bundle, subject="benzene")))


def test_an_unknown_job_fails_the_activity_naming_what_is_declared() -> None:
    """An unknown job fails the activity, naming what is declared.

    `ConnectorError` is a `ValueError`, which `BAD_DATA_RETRY` lists as non-retryable, so it fails
    on the first attempt.
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
    """The workflow module does not import the connector registry.

    The lookup is an activity so its answer is recorded in history. Checked against the module
    source rather than `sys.modules`, since the activity module legitimately imports the registry.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "durable" / "template_job.py"
    ).read_text()
    assert "chemclaw.connectors.registry" not in source, (
        "durable/template_job.py reaches the connector registry again; the lookup belongs in "
        "authorize_job_step so its answer is recorded in history (REV-13)"
    )


async def test_a_template_naming_an_unknown_job_fails_instead_of_hanging() -> None:
    """A template naming an unknown job fails instead of hanging, against a real server.

    The SDK's task-failure loop is what distinguishes "fails" from "hangs", so only a real server
    under a timeout can show it. Skips where the Temporal test server cannot be downloaded.
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

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[TemplateWorkflow],
            activities=[authorize_job_step, _swallow_record, _nothing_to_resume],
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


# --- DARK-2: the step is authorized and audited as its requester (D-168) -----------------------


# `TemplateWorkflow` records a `job_records` row on both paths, so the worker must serve the
# activity. A no-op here; `tests/test_template_job_record.py` owns the recording contract.
@activity.defn(name="record_job")
async def _swallow_record(record: Any) -> None:
    """Accept the run's durable record and discard it — this file is not about that write."""


@activity.defn(name="completed_steps")
async def _nothing_to_resume(request: Any) -> dict[str, Any]:
    """Answer the sequencer's resume read with "nothing", as a first run of an id gets.

    Served because `TemplateWorkflow` dispatches it before its first step.
    """
    return {}


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

    Written to disk and read through the real registry, since those two fields must survive the
    journey from manifest to launch. The conftest's autouse fixture clears the discovery cache.
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
    """An expensive job step is refused for an unentitled requester.

    `authorize_trigger` runs against the step's own requester before any child workflow starts, so a
    template cannot launch expensive work its runner could not launch directly.
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
    """A declared precondition runs on the template path too.

    The launch boundary is the only replay-safe place for it; a validator or a workflow check would
    re-run on replay against current config.
    """
    with pytest.raises(_PreconditionRefused):
        asyncio.run(authorize_job_step(_step(costly_bundle, subject="benzene")))


def test_the_launch_leaves_an_audit_row_naming_the_requester(
    monkeypatch: pytest.MonkeyPatch, fixture_bundle: str
) -> None:
    """A durable launch from a template leaves an audit row naming the requester.

    The row names the job, the person who asked and the run tying the steps together.
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
    """A step with bad arguments fails before any workflow starts.

    Otherwise a misspelled argument would fail somewhere inside the connector's workflow.
    """
    with pytest.raises(ValidationError):
        asyncio.run(authorize_job_step(_step(fixture_bundle, subjekt="benzene")))


def test_every_template_step_activity_is_registered_on_a_worker() -> None:
    """Every template step activity is registered on a worker.

    Asserted over all step kinds together, since the failure mode is a new kind arriving without its
    `@durable_activity` registration.
    """
    names = {activity.__name__ for activity in registered_activities("background")}
    assert {"authorize_job_step", "run_tool_step", "run_agent_step"} <= names, (
        f"template step activities missing from the background worker: "
        f"{sorted({'authorize_job_step', 'run_tool_step', 'run_agent_step'} - names)}"
    )


def test_the_run_is_started_with_a_whole_procedure_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run is started with a whole-procedure execution timeout.

    Otherwise its only bound is step budget × step count, which nothing declares and which grows
    with every added step. Asserted at the launch, since the setting existing is not the fix.
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
    """The run ceiling must be able to contain one step.

    A ceiling at or below the step budget would kill the procedure inside its first step with a bare
    `WorkflowExecutionTimedOut`; the config refuses it.
    """
    from chemclaw.core.config import Settings

    with pytest.raises(ValidationError) as caught:
        Settings(template_step_timeout_seconds=900.0, template_run_timeout_seconds=900.0)
    assert "template_run_timeout_seconds" in str(caught.value)


def test_the_run_ceiling_must_be_able_to_contain_one_job_step() -> None:
    """The run ceiling must be able to contain one `job` step.

    A `job` step is bounded by `wrapper_execution_timeout()`, not the step budget. An execution
    timeout is not delivered to workflow code, so outliving the ceiling ends the run silently and
    terminates the child before it records a failure. The inverted pair is refused, and the shipped
    defaults must clear the bound or every process would refuse at import.
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


def test_the_wrappers_headroom_covers_what_its_post_child_steps_may_spend() -> None:
    """The wrapper's headroom equals what its post-child steps may spend.

    The reservation is the sum of the steps' own budgets, computed from the call sites' helpers.
    Equality rather than `>=`: an added step would make `>=` truer while reserving nothing for it,
    and reserving too much wedges a long job just as reserving too little reaps it early.
    """
    from datetime import timedelta

    from chemclaw.durable.publish import light_write_queue_wait_timeout, queue_wait_timeout

    assert finish_headroom() == (
        # `_settle_effect`, `_publish_result` and the note write take core's hour...
        queue_wait_timeout() * 3
        # ...while the durable record, the push-back and the outbound copy take the tighter
        # end-of-job bound...
        + light_write_queue_wait_timeout() * 3
        # ...and each of the six then does its own work. The outbound copy's is
        # `delivery_timeout_seconds` rather than an activity's: it walks the channels serially.
        + timedelta(
            seconds=settings.activity_timeout_seconds * 2
            + settings.job_record_timeout_seconds
            + settings.result_publish_timeout_seconds
            + settings.note_write_timeout_seconds
            + settings.delivery_timeout_seconds
        )
    )
    assert wrapper_execution_timeout() == (
        timedelta(seconds=settings.connector_job_timeout_seconds) + finish_headroom()
    )


def test_the_configs_restatement_of_the_wrapper_ceiling_cannot_drift() -> None:
    """The config's restatement of the wrapper ceiling cannot drift.

    `core` may not import `durable`, so the config validator restates `wrapper_execution_timeout()`.
    Driven against `Settings` with the live function supplying the number, in both arms: the
    wrapper's own value is refused and one second past it is accepted, which pins equality.
    """
    from chemclaw.core.config import Settings

    wrapper = wrapper_execution_timeout().total_seconds()

    with pytest.raises(ValidationError) as caught:
        Settings(template_run_timeout_seconds=wrapper)
    assert "template_run_timeout_seconds" in str(caught.value)

    Settings(template_run_timeout_seconds=wrapper + 1)


async def test_a_failed_template_step_wakes_the_session_and_names_which_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed template step wakes the session and names which step.

    The chemist is told the procedure failed and where, since "the template failed" is unactionable
    for several steps, and the run must still fail rather than return an empty result. Driven
    against a real Temporal server, because the subject is the SDK's handling of an exception on the
    failure path. Skips where the test server cannot be downloaded.
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

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=_QUEUE,
            workflows=[TemplateWorkflow],
            activities=[
                authorize_job_step,
                record_session_event_activity,
                _swallow_record,
                _nothing_to_resume,
            ],
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

    assert len(notified) == 1, "a failed template emitted no session event at all — the defect"
    session_id, kind, payload = notified[0]
    assert (session_id, kind) == ("s-tmpl", "job_failed")
    assert payload["step"] == "first", (
        "which step failed is the one thing this workflow knows that the failure does not"
    )
    assert payload["template"] == "fails-midway"


async def test_a_declared_optional_input_the_caller_omitted_resolves_to_none() -> None:
    """A declared optional input the caller omitted resolves to `None`.

    Launch params are dumped with `exclude_none=True`, and templates reference optional inputs such
    as `${inputs.solvent}` unconditionally, so an omitted input must still be in scope. Driven
    through a real workflow, because the scope is built inside `TemplateWorkflow.run` from what the
    launcher hands it.
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

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[TemplateWorkflow],
            activities=[_agent, _swallow_record, _nothing_to_resume],
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

    # Reaching the step at all is the assertion: before this, the run died here.
    assert seen == ["solvent=null smiles=CCO"], (
        "an omitted optional input must resolve rather than raise; "
        "every shipped template references one unconditionally"
    )

    # A whole-string reference carries `None` itself, not "null" or "": the calc specs read `None`
    # as gas phase, and `unsupported([""])` rejects an empty string.
    assert resolve("${inputs.solvent}", {"inputs.solvent": None}) is None
