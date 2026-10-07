"""A template run leaves a durable `job_records` row.

Without it `find_past_jobs` cannot return the run, `get_durable_job_status` answers only while
Temporal retains the history, and a failed run leaves nothing. These tests drive the two pure
builders offline, the same split `job_record_for` takes; the workflow test needs a broker.
"""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chemclaw.durable.job_record import JobRecord
from chemclaw.durable.template_job import (
    TEMPLATE_JOB_FAMILY,
    TemplateRunInput,
    failed_template_record,
    template_fingerprint,
    template_job_record,
)
from chemclaw.templates.manifest import Template


def _template(name: str = "hazard-briefing") -> Template:
    """A minimal two-step template — enough to have a name, inputs and more than one result."""
    return Template.model_validate(
        {
            "name": name,
            "summary": "Screen a molecule for hazards and write a brief.",
            "inputs": [
                {"name": "smiles", "type": "string", "description": "The molecule, as SMILES."}
            ],
            "steps": [
                {
                    "id": "screen",
                    "kind": "tool",
                    "purpose": "The rule-table screen.",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": ["${inputs.smiles}"]},
                },
                {
                    "id": "write",
                    "kind": "agent",
                    "purpose": "Turn the flags into something a chemist can act on.",
                    "prompt": "Write a short brief for ${inputs.smiles}.",
                },
            ],
        }
    )


def _run(session_id: str = "sess-1") -> TemplateRunInput:
    """One launch of that template, as `templates/registry.py` builds it."""
    return TemplateRunInput(
        template=_template(),
        inputs={"smiles": "CCO"},
        requested_by="chemist@example.com",
        roles=["chemist"],
        session_id=session_id,
    )


def test_a_finished_template_run_records_what_it_ran_and_what_each_step_produced() -> None:
    """The row reconstructs the run without Temporal, the chat or the graph.

    Every step is kept, since a fixed procedure's value is showing what each stage produced.
    """
    results: dict[str, Any] = {"screen": {"flags": ["peroxide"]}, "write": "the brief text"}
    record = template_job_record("wf-1", _run(), results, "template 'hazard-briefing' completed")

    assert record.job_id == "wf-1"
    assert record.connector == TEMPLATE_JOB_FAMILY
    assert record.job == "hazard-briefing"
    assert record.requested_by == "chemist@example.com"
    assert record.session_id == "sess-1"
    assert record.payload == {"smiles": "CCO"}
    assert record.result == {"steps": results}
    assert record.state == "completed"
    # The brief itself is in the row, which is the whole point: it was unrecoverable before.
    assert "the brief text" in str(record.result)


def test_a_template_run_records_no_rationale_and_that_is_the_design() -> None:
    """A template run records no rationale, by design.

    The rationale field holds the requester's own words; a template is a reviewed procedure launched
    by name, and copying its `summary` there would assert an attribution nobody wrote.
    """
    assert template_job_record("wf-1", _run(), {}, "done").rationale == ""


def test_a_connector_job_still_cannot_be_recorded_without_a_rationale() -> None:
    """A connector job still cannot be launched without a rationale.

    The guarantee lives in `connectors/jobs.py`'s launcher, not in `JobRecord`; this fails if it is
    moved.
    """
    from chemclaw.connectors import jobs

    source = Path(jobs.__file__ or "")
    assert source.name, "could not locate connectors/jobs.py to assert its refusal"
    text = source.read_text(encoding="utf-8")
    assert "rationale must say why this run is being started" in text, (
        "the launcher-side rationale refusal is gone; JobRecord no longer backstops it"
    )


def test_a_failed_template_run_records_where_it_stopped_and_keeps_the_steps_that_ran() -> None:
    """A failed template run records where it stopped and keeps the steps that ran.

    `summary` stays empty and the reason goes in `failure_reason`, so a listing can tell a result
    from a failure.
    """
    completed: dict[str, Any] = {"screen": {"flags": []}}
    record = failed_template_record("wf-2", _run(), "write", "the model timed out", completed)

    assert record.state == "failed"
    assert record.summary == ""
    assert "write" in record.failure_reason
    assert "the model timed out" in record.failure_reason
    # Completed steps are what the next attempt resumes from, so the row records the template
    # version that produced them; a run's id hashes only the name and inputs.
    assert record.result == {
        "steps": completed,
        "template_fingerprint": template_fingerprint(_run().template),
    }


def test_a_step_stopped_by_a_cancellation_is_recorded_as_cancelled() -> None:
    """A step stopped by a cancellation is recorded as cancelled.

    A step reporting cancellation as its cause arrives as `_StepFailed`; `connector_job.ended_state`
    reads either form, so the row agrees with `GET /jobs/{id}`.
    """
    from temporalio.exceptions import CancelledError

    from chemclaw.durable.connector_job import ended_state

    record = failed_template_record(
        "wf-5", _run(), "write", "Cancelled", {}, state=ended_state(CancelledError("Cancelled"))
    )
    assert record.state == "cancelled"
    assert "write" in record.failure_reason


def test_both_records_name_the_run_as_its_own_correlation() -> None:
    """The record and the audit rows of the steps inside it join without a second identifier.

    `StepIdentity` already binds the workflow id as the correlation for every step, so using
    anything else here would give one run two ids and make the join a lookup.
    """
    for record in (
        template_job_record("wf-3", _run(), {}, "done"),
        failed_template_record("wf-3", _run(), "screen", "boom", {}),
    ):
        assert record.correlation_id == "wf-3"


def test_a_record_still_refuses_to_be_built_without_the_things_it_must_have() -> None:
    """Relaxing `rationale` did not relax the rest: no job or no requester is still not a row."""
    with pytest.raises(ValidationError):
        JobRecord(job_id="wf-4", connector="template", job="", requested_by="a")
    with pytest.raises(ValidationError):
        JobRecord(job_id="wf-4", connector="template", job="x", requested_by="")


def test_a_run_off_the_service_path_records_an_empty_session_rather_than_failing() -> None:
    """A template launched with no chat behind it is a real case, not an error."""
    assert template_job_record("wf-5", _run(session_id=""), {}, "done").session_id == ""


# --- the workflow actually writes it ------------------------------------------------------------


async def test_a_real_template_run_writes_the_row_and_a_failing_one_writes_its_own() -> None:
    """A real template run writes its row, and a failing one writes its own.

    Correct builders prove nothing if `TemplateWorkflow.run` never calls them. `record_job` is
    stubbed: the test is that the workflow asks for a record with the right content on both paths;
    `tests/test_job_record_postgres.py` owns the store.
    """
    from datetime import timedelta

    from temporalio import activity
    from temporalio.client import WorkflowFailureError
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.template_job import TemplateWorkflow
    from tests.temporal_env import pydantic_client, start_env_or_skip

    written: list[JobRecord] = []

    @activity.defn(name="record_job")
    async def _capture(record: JobRecord) -> None:
        written.append(record)

    @activity.defn(name="completed_steps")
    async def _resume(request: Any) -> dict[str, Any]:
        """Nothing to resume, which is what a first run of any id gets.

        Registered because the sequencer now asks this before its first step, so a rig that omits
        it measures an unserved activity rather than the record it is about.
        """
        return {}

    @activity.defn(name="run_agent_step")
    async def _agent(step: Any) -> str:
        # The activity is registered by name, so the payload arrives as the raw dict rather than
        # as `AgentStepInput` — the model is not imported here on purpose.
        if "boom" in str(step):
            raise ValueError("the model timed out")
        return "the brief text"

    def _one_agent_step(name: str, prompt: str) -> Template:
        return Template.model_validate(
            {
                "name": name,
                "summary": "One agent step.",
                "inputs": [],
                "steps": [{"id": "write", "kind": "agent", "purpose": "write", "prompt": prompt}],
            }
        )

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[TemplateWorkflow],
            activities=[_capture, _agent, _resume],
        ):
            await client.execute_workflow(
                TemplateWorkflow.run,
                TemplateRunInput(
                    template=_one_agent_step("good", "write it up"), requested_by="tester"
                ),
                id="template-record-ok",
                task_queue=settings.background_task_queue,
                execution_timeout=timedelta(seconds=60),
            )
            with pytest.raises(WorkflowFailureError):
                await client.execute_workflow(
                    TemplateWorkflow.run,
                    TemplateRunInput(
                        template=_one_agent_step("bad", "boom"), requested_by="tester"
                    ),
                    id="template-record-fail",
                    task_queue=settings.background_task_queue,
                    execution_timeout=timedelta(seconds=60),
                )

    assert len(written) == 2, (
        f"expected a record from the finished run and from the failed one, got {len(written)} — "
        "TemplateWorkflow has stopped recording on one of its two paths"
    )
    finished = next(r for r in written if r.state == "completed")
    failed = next(r for r in written if r.state == "failed")
    assert finished.job == "good"
    assert "the brief text" in str(finished.result)
    assert failed.job == "bad"
    assert "write" in failed.failure_reason


def test_a_cancelled_template_run_is_listed_as_cancelled_not_failed() -> None:
    """A cancelled template run is listed as cancelled, not failed.

    The step's `ActivityError` carries the `CancelledError` as its cause, which the workflow must
    not erase, so the broker, `GET /jobs/{id}` and `GET /jobs` agree. A cancellation surfacing bare
    also writes a row.
    """
    import asyncio
    from datetime import timedelta
    from unittest import mock

    from temporalio import activity
    from temporalio.worker import Worker

    from chemclaw.agent import durable_tools
    from chemclaw.core.config import settings
    from chemclaw.durable.template_job import TemplateWorkflow
    from tests.temporal_env import pydantic_client, start_local_env_or_skip

    written: list[JobRecord] = []

    @activity.defn(name="record_job")
    async def _capture(record: JobRecord) -> None:
        written.append(record)

    @activity.defn(name="completed_steps")
    async def _resume(request: Any) -> dict[str, Any]:
        return {}

    @activity.defn(name="run_agent_step")
    async def _agent(step: Any) -> str:
        # Runs until the worker shuts down: only a cancellation ends this run.
        await asyncio.sleep(3600)
        return "never"

    async def _lookup(job_id: str) -> JobRecord | None:
        return next((r for r in written if r.job_id == job_id), None)

    template = Template.model_validate(
        {
            "name": "stoppable",
            "summary": "One agent step that never finishes.",
            "inputs": [],
            "steps": [{"id": "write", "kind": "agent", "purpose": "write", "prompt": "go"}],
        }
    )

    async def _run() -> tuple[str, str, str]:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[TemplateWorkflow],
                activities=[_capture, _agent, _resume],
            ):
                handle = await client.start_workflow(
                    TemplateWorkflow.run,
                    TemplateRunInput(template=template, requested_by="tester"),
                    id="template-cancel-probe",
                    task_queue=settings.background_task_queue,
                    execution_timeout=timedelta(seconds=120),
                )
                deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < deadline:
                    pending = (await handle.describe()).raw_description.pending_activities
                    if any(a.activity_type.name == "run_agent_step" for a in pending):
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise AssertionError("the step never started")
                await handle.cancel()
                deadline = asyncio.get_running_loop().time() + 60
                while (described := await handle.describe()).status is not None and (
                    described.status.name == "RUNNING"
                ):
                    assert asyncio.get_running_loop().time() < deadline, "never left RUNNING"
                    await asyncio.sleep(0.2)
                with mock.patch.object(
                    durable_tools, "connect", mock.AsyncMock(return_value=client)
                ):
                    live = await durable_tools.job_status(handle.id, wait_seconds=0.0)
            with mock.patch.object(durable_tools, "lookup_job_record", _lookup):
                stored = await durable_tools._recorded_status("template-cancel-probe")
            assert stored is not None, "the cancelled run wrote no job_records row"
            assert described.status is not None
            return described.status.name, live.status, stored.status

    broker, detail, listed = asyncio.run(_run())
    assert broker == "CANCELED"
    assert detail == "cancelled"
    assert listed == detail
    assert [record.state for record in written] == ["cancelled"]
    assert "write" in written[0].failure_reason


def test_a_relaunch_after_a_cancel_resumes_the_steps_that_finished() -> None:
    """Cancel after step one finished, relaunch the same id: step one is not run again.

    Resume must accept a cancelled run as well as a failed one, since a finished step is never
    recomputed.
    """
    import asyncio
    from datetime import timedelta
    from unittest import mock

    from temporalio import activity
    from temporalio.client import WorkflowFailureError
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.worker import Worker

    from chemclaw.core.config import settings
    from chemclaw.durable.template_activities import completed_steps
    from chemclaw.durable.template_job import TemplateWorkflow
    from tests.temporal_env import pydantic_client, start_local_env_or_skip

    written: list[JobRecord] = []
    ran: list[str] = []
    second_launch = asyncio.Event()

    @activity.defn(name="record_job")
    async def _capture(record: JobRecord) -> None:
        written.append(record)

    async def _lookup(job_id: str) -> JobRecord | None:
        return next((r for r in reversed(written) if r.job_id == job_id), None)

    @activity.defn(name="run_agent_step")
    async def _agent(step: Any) -> str:
        prompt = str(step)
        if "first" in prompt:
            ran.append("one")
            return "step one's result"
        ran.append("two")
        if not second_launch.is_set():
            await asyncio.sleep(3600)  # only the cancel ends the first launch
        return "step two's result"

    template = Template.model_validate(
        {
            "name": "resumable",
            "summary": "Two chained agent steps.",
            "inputs": [],
            "steps": [
                {"id": "one", "kind": "agent", "purpose": "first", "prompt": "first"},
                {
                    "id": "two",
                    "kind": "agent",
                    "purpose": "second",
                    "prompt": "second, after ${steps.one.result}",
                },
            ],
        }
    )
    job_id = "template-resume-after-cancel"

    async def _run() -> Any:
        async with await start_local_env_or_skip() as env:
            client = pydantic_client(env)
            with mock.patch("chemclaw.durable.job_record.lookup_job_record", _lookup):
                async with Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    workflows=[TemplateWorkflow],
                    activities=[_capture, _agent, completed_steps],
                ):
                    launch: dict[str, Any] = {
                        "id": job_id,
                        "task_queue": settings.background_task_queue,
                        "execution_timeout": timedelta(seconds=120),
                        "id_reuse_policy": WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    }
                    run = TemplateRunInput(template=template, requested_by="tester")
                    handle = await client.start_workflow(TemplateWorkflow.run, run, **launch)
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + 30
                    while "two" not in ran:
                        assert loop.time() < deadline, "step two never started"
                        await asyncio.sleep(0.2)
                    await handle.cancel()
                    with pytest.raises(WorkflowFailureError):
                        await handle.result()
                    second_launch.set()
                    return await client.execute_workflow(TemplateWorkflow.run, run, **launch)

    result = asyncio.run(_run())
    assert written[0].state == "cancelled"
    assert written[0].result["steps"] == {"one": "step one's result"}
    # Step one ran once, on the first launch; the relaunch ran only step two.
    assert ran == ["one", "two", "two"], ran
    assert result.steps["one"] == "step one's result"
    assert written[-1].state == "completed"


# --- what the resume read will and will not hand back --------------------------------------------


async def test_the_resume_read_answers_only_for_a_failed_run_of_the_same_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume answers only for a failed run of the same template.

    `job_records` is upserted on `job_id`, so a completed run would otherwise resume itself, and an
    edited template relaunches under the same id with a different procedure.
    """
    from chemclaw.core.config import settings
    from chemclaw.durable.job_record import record_job
    from chemclaw.durable.template_activities import ResumeRequest, completed_steps
    from tests.pg import migrated_db_or_skip

    # Durable records follow the session store's switch (`job_record._records_are_durable`), and
    # the default is `memory` — under which both the write and the read are no-ops and every
    # assertion below would pass for the wrong reason.
    monkeypatch.setattr(settings, "session_store", "postgres")

    await migrated_db_or_skip()
    job_id = f"template-resume-probe-{uuid4().hex[:12]}"
    steps = {"one": {"ok": "two"}}

    failed = JobRecord(
        job_id=job_id,
        connector="template",
        job="probe",
        requested_by="tester",
        correlation_id=job_id,
        payload={"smiles": "CCO"},
        result={"steps": steps, "template_fingerprint": "fp-1"},
        payload_kind="template",
        state="failed",
        failure_reason="step 'two': boom",
    )
    await record_job(failed)

    assert await completed_steps(ResumeRequest(job_id=job_id, fingerprint="fp-1")) == steps
    # A cancelled run's finished steps are as real as a failed one's, and the id relaunches after
    # a cancel too (`ALLOW_DUPLICATE_FAILED_ONLY`).
    await record_job(failed.model_copy(update={"state": "cancelled"}))
    assert await completed_steps(ResumeRequest(job_id=job_id, fingerprint="fp-1")) == steps
    await record_job(failed)
    # A different definition under the same id.
    assert await completed_steps(ResumeRequest(job_id=job_id, fingerprint="fp-2")) == {}
    # An id nothing has ever recorded.
    assert await completed_steps(ResumeRequest(job_id="nope", fingerprint="fp-1")) == {}

    # And once the run succeeds, its row is upserted to `completed` — which must not read back
    # as something to resume, or every re-ask of a finished procedure would skip its own work.
    await record_job(failed.model_copy(update={"state": "completed"}))
    assert await completed_steps(ResumeRequest(job_id=job_id, fingerprint="fp-1")) == {}
