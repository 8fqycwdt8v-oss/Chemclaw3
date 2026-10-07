"""Running a template: the deterministic sequencer, and the reason a template is durable at all.

The workflow substitutes references (pure), dispatches each step to an activity or child workflow,
accumulates results and pushes back to the launching session. The resolved template travels in
the input, not its name, so editing `data/templates/<name>.yaml` cannot change a run in flight —
a replay requirement as well as the versioning story. Identity travels too and is stamped by
each activity (`chemclaw.durable.template_activities`), so every step is authorized against the
requester exactly as a chat turn is.
"""

import asyncio
import contextlib
from datetime import timedelta
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import (
        ConnectorJobInput,
        ConnectorJobResult,
        child_workflow_id,
        ended_state,
        failure_reason,
        wrapper_execution_timeout,
    )
    from chemclaw.durable.job_record import JobRecord, record_job
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.template_activities import (
        AgentStepInput,
        AgentStepResult,
        JobStepInput,
        ResumeRequest,
        StepIdentity,
        ToolStepInput,
        authorize_job_step,
        completed_steps,
        run_agent_step,
        run_tool_step,
    )
    from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep
    from chemclaw.templates.resolve import resolve
    from chemclaw.templates.schedule import batches, schedule

from chemclaw.core.ids import stable_hash
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    agent_step_retry,
    light_write_queue_wait_timeout,
    queue_wait_timeout,
)
from chemclaw.durable.registry import durable_workflow


class TemplateRunInput(BaseModel):
    """One template run: the pinned definition, its arguments, and whose run it is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The *resolved* template, not a name — see the module docstring. This is what makes an edit
    # safe and a replay deterministic.
    template: Template
    inputs: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    # The chat to wake on completion; empty off the service path, where there is none.
    session_id: str = ""
    # How many of one wave's steps may be in flight at once, pinned at launch: the launch-time
    # ceiling check (`agent/template_surface.run_ceiling_problems`) sized the run with this number,
    # and a settings read in workflow code would be nondeterministic. `0` means no bound (older
    # inputs, whose waves are one step wide).
    max_parallel_steps: int = Field(default=0, ge=0)


class TemplateRunResult(BaseModel):
    """What a finished run produced: every step's result, and the last one as the answer.

    Every step is kept, not just the last, because the point of a fixed procedure is being able to
    show what each stage produced — that is what an auditor asks for, and reconstructing it from
    Temporal history afterwards is not something a chemist can do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    template: str
    steps: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class _StepFailed(Exception):
    """Which step of a wave failed, and why — the pairing a concurrent wave would otherwise lose.

    Internal only: the caller unwraps it and re-raises the cause, so Temporal's view of the failure
    is the original exception.
    """

    def __init__(self, step: Any, cause: BaseException) -> None:
        super().__init__(f"template step {getattr(step, 'id', '?')!r} failed")
        self.step = step
        self.cause = cause


# On the light queue: the sequencer only substitutes and dispatches; the weight is in the steps.
#
# `failure_exception_types=[Exception]` so the sequencer's own plain exceptions (unknown step kind,
# unresolved reference) fail the run instead of suspending it in the SDK's endless task-failure
# retry loop. Retry classification at activity boundaries is `BAD_DATA_RETRY`'s job.
#
# The family a template run occupies in `job_records.connector`, which `find_past_jobs` filters on.
# A literal, because it is stored in rows; changing it would orphan them.
TEMPLATE_JOB_FAMILY = "template"


def template_fingerprint(template: "Template") -> str:
    """What a resumable run's completed steps belong to — the resolved template, hashed.

    The run id hashes only name and inputs (`templates/registry.run_workflow_id`), so an edited
    template relaunched with the same inputs shares the id; this lets `completed_steps` tell the two
    procedures apart.
    """
    return stable_hash(template.model_dump(mode="json"))


def run_summary(template: str, steps: int, degradations: dict[str, str]) -> str:
    """The one sentence a listing shows for a finished run — **including what it ran without**.

    Listings render only `summary`, so degraded steps are named here by id. `state` stays
    `completed`: the run did finish, and `job_records.state` is a two-value column.
    """
    line = f"template {template!r} completed {steps} step(s)"
    if not degradations:
        return line
    return f"{line} — DEGRADED at {', '.join(sorted(degradations))}"


def template_job_record(
    job_id: str,
    run: "TemplateRunInput",
    results: dict[str, Any],
    summary: str,
    degradations: dict[str, str] | None = None,
) -> JobRecord:
    """The durable record of one finished template run.

    Makes a run findable by `find_past_jobs` and `get_durable_job_status` after Temporal's history
    expires. Pure, so the offline suite can test it. `rationale` is empty: the row names the
    template, whose own `summary` states its purpose, and the field is for the requester's words.

    Args:
        job_id: The run's workflow id, which is also its correlation id.
        run: The pinned template, its inputs, and whose run it is.
        results: Every step's result, keyed by step id.
        summary: The run's one-line account — build it with `run_summary`.
        degradations: Which steps ran with something missing, and what, keyed by step id. `None`
            or empty for a clean run, which is what keeps such a run's row unchanged.
    """
    return JobRecord(
        job_id=job_id,
        connector=TEMPLATE_JOB_FAMILY,
        job=run.template.name,
        requested_by=run.requested_by,
        session_id=run.session_id,
        # The run *is* the correlation — the same identity `StepIdentity` binds for every step, so
        # the record and the audit rows of the steps inside it join without a second identifier.
        correlation_id=job_id,
        payload=dict(run.inputs),
        summary=summary,
        # Every step, not only the last: showing what each stage produced is the point of a
        # procedure. `degraded` sits beside them, keyed by step id and present only when non-empty,
        # so a clean run's result is unchanged; the step text carries the notice too, for the human
        # reader.
        result=(
            {"steps": results, "degraded": degradations} if degradations else {"steps": results}
        ),
        payload_kind="template",
    )


def failed_template_record(
    job_id: str,
    run: "TemplateRunInput",
    step_id: str,
    reason: str,
    completed: dict[str, Any],
    *,
    state: str = "failed",
) -> JobRecord:
    """The record of a template run that ended badly — what was asked, where it stopped, and why.

    `summary` stays empty; the reason goes in `failure_reason`. Completed steps are kept so the work
    is not lost (and a relaunch can resume from them). `state` is `connector_job.ended_state` of the
    exception, so a cancelled step is booked `cancelled`, not `failed`.
    """
    return JobRecord(
        job_id=job_id,
        connector=TEMPLATE_JOB_FAMILY,
        job=run.template.name,
        requested_by=run.requested_by,
        session_id=run.session_id,
        correlation_id=job_id,
        payload=dict(run.inputs),
        # The fingerprint travels with the steps, so a reader never has to look elsewhere for what
        # they belong to.
        result={"steps": completed, "template_fingerprint": template_fingerprint(run.template)},
        payload_kind="template",
        state=state,
        failure_reason=f"step {step_id!r}: {reason}",
    )


@durable_workflow("background")
@workflow.defn(failure_exception_types=[Exception])
class TemplateWorkflow:
    """Run a template's steps in order, durably, and return every step's result."""

    @workflow.run
    async def run(self, run: TemplateRunInput) -> TemplateRunResult:
        """Substitute, dispatch, accumulate — once per step, in the declared order."""
        timeout = timedelta(seconds=settings.template_step_timeout_seconds)
        identity = StepIdentity(
            actor=run.requested_by,
            roles=list(run.roles),
            # The run *is* the correlation, so its own workflow id is the id that ties its steps
            # together in the audit trail — no second identifier to generate or reconcile.
            correlation_id=workflow.info().workflow_id,
            # The launching chat, carried to every step so audit rows book the conversation. Empty
            # off the service path.
            session_id=run.session_id,
        )
        # Every *declared* input is in scope, defaulting to `None`, because the params are dumped
        # with `exclude_none=True` and an omitted optional input (e.g. `solvent`, read as gas phase)
        # must still resolve. A reference to an undeclared input is refused at template load time.
        scope: dict[str, Any] = {f"inputs.{item.name}": None for item in run.template.inputs}
        scope.update({f"inputs.{key}": value for key, value in run.inputs.items()})
        # One patch marker gates both waves and resume: off the marker nothing is resumed and each
        # wave is one step, awaited directly, issuing the same commands as before, so open histories
        # replay (`tests/test_workflow_replay.py`). Drainable once every run open at that release
        # has ended (bounded by `template_run_timeout_seconds`); then deprecate and delete per
        # `docs/guides/workflow-versioning.md`. Never reuse the id.
        scheduled = workflow.patched("template-waves-and-resume")
        # Steps a previous failed or cancelled run of this id already finished; empty otherwise.
        # Read by an activity because it is a database read that decides how many activities follow.
        results: dict[str, Any] = await self._resume(run) if scheduled else {}
        scope.update({f"steps.{step_id}.result": value for step_id, value in results.items()})
        # Which steps ran degraded, and how, keyed by step id. Empty for a clean run, which keeps
        # its record and push-back unchanged.
        degradations: dict[str, str] = {}

        # Waves derived from the `${steps.<id>.result}` edges the file declares
        # (`templates/schedule.py`); a chained template yields one step per wave.
        waves = (
            schedule(run.template)
            if scheduled
            # The pre-marker shape, written in the new code's own terms rather than kept as a
            # second loop: one step per wave *is* the old sequencer.
            else tuple((step,) for step in run.template.steps)
        )
        for wave in waves:
            # A resumed step is not re-dispatched; its result is already in `scope`.
            wave = tuple(step for step in wave if step.id not in results)
            if not wave:
                continue
            try:
                finished = await self._run_wave(
                    wave, scope, identity, timeout, run.template.name, run.max_parallel_steps
                )
            except asyncio.CancelledError as cancelled:
                # A cancelled run still writes a `cancelled` row: record, announce, then re-raise,
                # as `ConnectorJobWorkflow` does, so the run closes CANCELED and listing and detail
                # agree. Skipped on eviction, which is not a cancellation and has no event loop (see
                # `ConnectorJobWorkflow.run`). No patch marker needed: a cancel is handled in the
                # task that delivers it.
                if not workflow.in_workflow():
                    raise
                step = wave[0]
                await self._record_run(
                    failed_template_record(
                        workflow.info().workflow_id,
                        run,
                        step.id,
                        failure_reason(cancelled),
                        dict(results),
                        state="cancelled",
                    )
                )
                await self._notify_failure(run, step, cancelled)
                raise
            except _StepFailed as failure:
                step, exc = failure.step, failure.cause
                # Record first, then tell the session which step failed, mirroring
                # `connector_job._notify_failure`; both are best effort because the run is already
                # failing.
                ended = ended_state(exc)
                await self._record_run(
                    failed_template_record(
                        workflow.info().workflow_id,
                        run,
                        step.id,
                        failure_reason(exc),
                        dict(results),
                        state=ended,
                    )
                )
                await self._notify_failure(run, step, exc)
                if ended == "cancelled":
                    # Raised with its cause so the SDK sees the `CancelledError` and closes the run
                    # CANCELED, not FAILED; `from None` would erase it.
                    raise exc from exc.__cause__
                raise exc from None
            # Folded in the wave's declared order, never completion order, so execution and replay
            # build the same `results` and `scope`.
            for step, result in finished:
                # Unwrapped here so `scope` and `results` hold text like every other step kind;
                # `step_value()` carries the degradation notice into that text, since the next step
                # reads nothing else.
                if isinstance(result, AgentStepResult):
                    if result.degraded:
                        degradations[step.id] = result.notice()
                    result = result.step_value()
                results[step.id] = result
                scope[f"steps.{step.id}.result"] = result

        summary = run_summary(run.template.name, len(run.template.steps), degradations)
        # Recorded before the push-back, so the id the chemist receives is already answerable. Best
        # effort:
        # losing the row must not fail a finished run.
        await self._record_run(
            template_job_record(workflow.info().workflow_id, run, results, summary, degradations)
        )
        if run.session_id:
            await notify_session_best_effort(
                run.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "template": run.template.name,
                    "summary": summary,
                    # Only when there is one, so a clean run's push-back is the payload it has
                    # always been and the UI's `normalizeEvent` has nothing new to drop.
                    **({"degraded": degradations} if degradations else {}),
                },
            )
        # The last step's result is the run's answer: a procedure ends with the thing it was for,
        # and a caller that wants an earlier stage has every one of them in `steps`.
        last = run.template.steps[-1].id
        return TemplateRunResult(template=run.template.name, steps=results, result=results[last])

    async def _resume(self, run: TemplateRunInput) -> dict[str, Any]:
        """The steps a previous failed or cancelled attempt at this id already finished.

        `ALLOW_DUPLICATE_FAILED_ONLY` lets an id re-run after a run that did not complete; without
        this it would redo every step. Best effort: an unreadable record yields `{}` and the run
        starts over. Unconditional, since skipping a step whose side effects already happened is the
        conservative choice.

        Args:
            run: This execution's pinned template and inputs.

        Returns:
            `{step_id: result}`, empty when there is nothing to resume.
        """
        return cast(
            dict[str, Any],
            await workflow.execute_activity(
                completed_steps,
                ResumeRequest(
                    job_id=workflow.info().workflow_id,
                    fingerprint=template_fingerprint(run.template),
                ),
                task_queue=settings.background_task_queue,
                start_to_close_timeout=timedelta(seconds=settings.job_record_timeout_seconds),
                # The light write's queue wait: a resume nobody serves promptly is worth abandoning,
                # since starting over is correct (`tests/test_activity_queue_bound.py` requires a
                # bound).
                schedule_to_start_timeout=light_write_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            ),
        )

    async def _record_run(self, record: JobRecord) -> None:
        """Persist the run's durable record, logging rather than failing the run if it cannot be.

        Mirrors `connector_job.ConnectorJobWorkflow._record_run`. The queue is named explicitly
        because `record_job` is registered only on the background queue.
        """
        try:
            await workflow.execute_activity(
                record_job,
                record,
                task_queue=settings.background_task_queue,
                start_to_close_timeout=timedelta(seconds=settings.job_record_timeout_seconds),
                # `start_to_close` starts counting only when a worker picks the task up, so the
                # queue wait needs its own bound or an unserved queue hangs the run
                # (`tests/test_activity_queue_bound.py`). The light-write wait, as
                # `connector_job._record_run` and `durable/notify.py` use: a total schedule-to-close
                # would be spent on a busy shared queue and cap all retries together.
                schedule_to_start_timeout=light_write_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
        except Exception:
            workflow.logger.warning(
                "could not record template run %s (%s); the run itself is unaffected",
                record.job_id,
                record.job,
            )

    async def _notify_failure(self, run: TemplateRunInput, step: Any, exc: BaseException) -> None:
        """Tell the session which step failed, before the failure propagates and closes this run.

        Never raises: on the way out of a failing workflow an exception here would replace the
        original failure. Catches `BaseException` because `notify_session_best_effort` may raise
        `CancelledError`; the caller re-raises the original.
        """
        if not run.session_id:
            return
        with contextlib.suppress(BaseException):
            await notify_session_best_effort(
                run.session_id,
                "job_failed",
                {
                    "job_id": workflow.info().workflow_id,
                    "template": run.template.name,
                    "step": getattr(step, "id", ""),
                    "reason": failure_reason(exc),
                },
            )

    async def _run_wave(
        self,
        wave: tuple[Any, ...],
        scope: dict[str, Any],
        identity: StepIdentity,
        timeout: timedelta,
        template: str,
        limit: int,
    ) -> list[tuple[Any, Any]]:
        """Run one wave's steps together and return `(step, result)` in the wave's declared order.

        A wave of one is awaited directly, so chained templates create no task and keep their
        history. Uses `gather(return_exceptions=True)`, the repository's fan-out shape, rather than
        cancelling siblings on first failure: cancels are commands, so the command count would
        depend on which branch lost; a sibling's wait is bounded by its own timeout. A
        `CancelledError` is re-raised as control flow, not recorded as a step failure. The first
        failure in declared order is reported, so the record never depends on timing.

        Args:
            wave: The steps to run together, in the file's order.
            scope: References resolved so far. Read only — a wave's steps cannot see each other.
            identity: Who the run acts for.
            timeout: One step's `start_to_close` budget.
            template: The run's template name, for the prompt-truncation label.
            limit: How many steps may be in flight at once; `0` for no bound. See
                `templates/schedule.batches` and `TemplateRunInput.max_parallel_steps`.

        Returns:
            `(step, result)` for each step, in the wave's declared order.

        Raises:
            _StepFailed: Carrying the step that failed and its cause, so the caller can write the
                record and the push-back that name it.
        """
        if len(wave) == 1:
            step = wave[0]
            try:
                return [(step, await self._run_step(step, scope, identity, timeout, template))]
            except asyncio.CancelledError:
                # Re-raised, not wrapped: a cancellation is not a failure at a step.
                raise
            except BaseException as exc:
                raise _StepFailed(step, exc) from exc

        done: list[tuple[Any, Any]] = []
        for batch in batches(wave, limit):
            settled = await asyncio.gather(
                *(self._run_step(step, scope, identity, timeout, template) for step in batch),
                return_exceptions=True,
            )
            # `gather` returns in argument order, and batches run in declared order, so the reported
            # failure is the first in the file regardless of timing.
            for step, outcome in zip(batch, settled, strict=True):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, BaseException):
                    raise _StepFailed(step, outcome) from outcome
            done.extend(zip(batch, settled, strict=True))
        return done

    async def _run_step(
        self,
        step: Any,
        scope: dict[str, Any],
        identity: StepIdentity,
        timeout: timedelta,
        template: str,
    ) -> Any:
        """Dispatch one step on its kind, with its references already substituted.

        `template` only labels the prompt-truncation counter; it decides nothing a step does.
        """
        # Both dispatched activities heartbeat, so a worker killed mid-step is detected in a beat,
        # not after the whole step budget. Not on the `job` step: local activities cannot heartbeat,
        # and it is a cached in-process lookup.
        heartbeat = timedelta(seconds=settings.template_step_heartbeat_timeout_seconds)
        if isinstance(step, ToolStep):
            return await workflow.execute_activity(
                run_tool_step,
                ToolStepInput(
                    tool=step.tool, arguments=resolve(step.arguments, scope), identity=identity
                ),
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=heartbeat,
                retry_policy=BAD_DATA_RETRY,
            )
        if isinstance(step, AgentStep):
            # Not on `BAD_DATA_RETRY`: an activity has no checkpointer, so a retried agent step
            # re-runs the whole turn and repeats every side effect. The SDK's own `llm_max_retries`
            # absorbs blips; see `publish.agent_step_retry`.
            return await workflow.execute_activity(
                run_agent_step,
                AgentStepInput(
                    prompt=resolve(step.prompt, scope),
                    profile=step.profile,
                    # Declared writes come from the pinned template, so editing the file cannot
                    # widen a run in flight.
                    write_tools=step.write_tools,
                    identity=identity,
                    # Which step this is, so each agent step's cost row is distinguishable within a
                    # run that shares one correlation id.
                    step_id=step.id,
                    # Labels the truncation counter below and nothing else.
                    template=template,
                ),
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=heartbeat,
                retry_policy=agent_step_retry(),
            )
        if isinstance(step, JobStep):
            return await self._run_job_step(step, scope, identity, timeout)
        raise ValueError(f"unknown template step kind {type(step).__name__}")

    async def _run_job_step(
        self,
        step: JobStep,
        scope: dict[str, Any],
        identity: StepIdentity,
        timeout: timedelta,
    ) -> ConnectorJobResult:
        """Run a connector job as a child workflow and await it — the whole point of a `job` step.

        A template sequences work, so it waits rather than returning a job id. Going through
        `ConnectorJobWorkflow` keeps the job's cross-cutting concerns (note write, attribution) in
        one place.
        """
        # Resolved by a local activity so the answer is recorded in history: resolving reads bundles
        # off disk, and an unknown job name raised in workflow code would suspend the run forever
        # instead of failing it. The activity also authorizes the step as the requester and returns
        # the validated payload, which is what the child is started with.
        resolved = await workflow.execute_local_activity(
            authorize_job_step,
            JobStepInput(
                job=step.job,
                arguments=resolve(step.arguments, scope),
                identity=identity,
            ),
            start_to_close_timeout=timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        # Addressed by type name, so the child's return is untyped at the call site; `result_type`
        # is what actually decodes it into a `ConnectorJobResult`.
        return cast(
            ConnectorJobResult,
            await workflow.execute_child_workflow(
                "ConnectorJobWorkflow",
                ConnectorJobInput(
                    connector=resolved.connector,
                    job=resolved.job,
                    workflow=resolved.workflow,
                    task_queue=resolved.task_queue,
                    # The validated payload the authorizing activity returned, never the raw
                    # arguments.
                    payload=resolved.payload,
                    # The step's declared purpose is the rationale; a step without one gets a
                    # non-blank deterministic fallback, since `ConnectorJobInput` requires one.
                    rationale=step.purpose or f"template step {step.id!r} (job {step.job})",
                    requested_by=identity.actor,
                    # Both ids are required downstream: `ConnectorJobWorkflow._notify_failure` skips
                    # a job with no session, and the correlation id joins the job back to this run.
                    session_id=identity.session_id,
                    correlation_id=identity.correlation_id,
                    # The job's own declared ceiling, as on the chat path.
                    timeout_seconds=resolved.timeout_seconds,
                    # A job that waits on a person gets no child ceiling
                    # (`child_execution_timeout`). The wrapper's `execution_timeout` below is a
                    # separate, step-level bound (`wrapper_execution_timeout`).
                    awaits_answer=resolved.awaits_answer,
                    publish_to_graph=resolved.publish_to_graph,
                ),
                # Named from the run's execution, not just its id: `ALLOW_DUPLICATE_FAILED_ONLY`
                # re-executes under the same id, and a step-id-only child id would collide. See
                # `child_workflow_id`.
                id=child_workflow_id(step.id),
                task_queue=settings.background_task_queue,
                result_type=ConnectorJobResult,
                # Still reject-duplicate: within one template execution two steps must never
                # collide on an id, which is a template-authoring bug worth failing loudly on.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                # One attempt, as in `ConnectorJobWorkflow._run_child`: at a child-workflow boundary
                # the outermost failure is a child failure, so `BAD_DATA_RETRY` classifies nothing
                # and would rerun the whole job up to five times. The child's own activities carry
                # `BAD_DATA_RETRY`, where classification works.
                retry_policy=RetryPolicy(maximum_attempts=1),
                # Bounded by the wrapper's ceiling, which must exceed the child's: an execution
                # timeout is not delivered to workflow code, so if the wrapper expired first there
                # would be no push-back and no record. See `wrapper_execution_timeout`.
                execution_timeout=wrapper_execution_timeout(),
            ),
        )
