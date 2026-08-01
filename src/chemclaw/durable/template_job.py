"""Running a template: the deterministic sequencer, and the reason a template is durable at all.

A template's whole value is that the order does not vary — so the thing that walks the steps must be
the one part of the system that is *replayable*. That is this workflow: it substitutes references
(pure), dispatches each step to an activity or a child workflow (I/O), accumulates results, and
pushes back to the launching session at the end.

**The resolved template travels in the input.** Not its name — the template itself. Editing
`data/templates/<name>.yaml` therefore cannot change a run already in flight, which is both the
versioning story (`src/chemclaw/templates/README.md`) and a hard replay requirement: a workflow
that re-read a file on replay would take a different path than its own history and Temporal would
refuse it.
Pinning the definition makes "edit a template" a safe, boring operation with no migration.

**Identity travels too**, and is stamped by each activity before the work happens
(`chemclaw.durable.template_activities`). A template must not be a way to run something the
requester could
not run themselves, so every step is authorized against the same actor, through the same gate, as a
chat turn.
"""

from datetime import timedelta
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import (
        ConnectorJobInput,
        ConnectorJobResult,
        child_workflow_id,
    )
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.template_activities import (
        AgentStepInput,
        JobStepInput,
        StepIdentity,
        ToolStepInput,
        authorize_job_step,
        run_agent_step,
        run_tool_step,
    )
    from chemclaw.templates.manifest import AgentStep, JobStep, Template, ToolStep
    from chemclaw.templates.resolve import resolve

from chemclaw.durable.publish import BAD_DATA_RETRY
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


# On the light queue: the sequencer only substitutes references and dispatches. Whatever
# weight a step carries is the tool's, the job's child workflow's, or the model turn's.
#
# `failure_exception_types` because the sequencer raises plain exceptions of its own — an unknown
# step kind, a reference that resolves to nothing. Without it the Temporal SDK treats any raw
# exception from workflow code as a suspected bug and suspends the run in an internal task-failure
# retry loop that ignores the retry policy and never gives up, so a template that can *never*
# succeed hangs instead of failing (the trap D-093 documents for fan-out children; REV-13 found the
# same hole here). Scoped to `Exception` rather than a name list because the classification that
# matters at an *activity* boundary — which errors are worth retrying — is already made by
# `BAD_DATA_RETRY`; what this decides is only whether the workflow is allowed to fail at all, and
# the answer is always yes.
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
        )
        scope: dict[str, Any] = {f"inputs.{key}": value for key, value in run.inputs.items()}
        results: dict[str, Any] = {}

        for step in run.template.steps:
            result = await self._run_step(step, scope, identity, timeout)
            results[step.id] = result
            scope[f"steps.{step.id}.result"] = result

        if run.session_id:
            await notify_session_best_effort(
                run.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "template": run.template.name,
                    "summary": f"template {run.template.name!r} completed "
                    f"{len(run.template.steps)} step(s)",
                },
            )
        # The last step's result is the run's answer: a procedure ends with the thing it was for,
        # and a caller that wants an earlier stage has every one of them in `steps`.
        last = run.template.steps[-1].id
        return TemplateRunResult(template=run.template.name, steps=results, result=results[last])

    async def _run_step(
        self, step: Any, scope: dict[str, Any], identity: StepIdentity, timeout: timedelta
    ) -> Any:
        """Dispatch one step on its kind, with its references already substituted."""
        if isinstance(step, ToolStep):
            return await workflow.execute_activity(
                run_tool_step,
                ToolStepInput(
                    tool=step.tool, arguments=resolve(step.arguments, scope), identity=identity
                ),
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
            )
        if isinstance(step, AgentStep):
            return await workflow.execute_activity(
                run_agent_step,
                AgentStepInput(
                    prompt=resolve(step.prompt, scope),
                    profile=step.profile,
                    identity=identity,
                ),
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
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

        A `tool` step naming a job launcher would return an id and move on, which is right in a chat
        turn (the agent must not block) and useless here: a template exists to sequence work, so it
        waits. Reusing `ConnectorJobWorkflow` rather than starting the connector's workflow directly
        keeps the job's cross-cutting concerns — the PR-gate publish, the actor attribution — in the
        one place that owns them.
        """
        # Through an activity, not by calling `find_job` here: the lookup reads the connector
        # bundles off disk, so doing it in workflow code made the child-workflow start depend on the
        # replaying worker's filesystem rather than on history — and an unknown job name raised a
        # plain `ValueError` into workflow code, which Temporal retries as a suspected bug forever
        # instead of failing the run (REV-13). Local, because it is a cached in-process lookup, not
        # a network call; the point is recording the answer, not offloading the work.
        #
        # It also *authorizes* the step, as the run's requester, and returns the validated payload
        # (D-168). The arguments are handed to it rather than substituted into the child start
        # below, because a payload that has not been through `prepare_job_launch` is precisely what
        # this step used to start an HPC job with.
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
                    # The *validated* payload the authorizing activity returned, never the raw
                    # arguments — see `authorize_job_step` (D-168).
                    payload=resolved.payload,
                    # A template already declares why each of its steps exists, so the run's
                    # rationale (D-157) is that declaration rather than a second field an author
                    # would have to write twice. A step with no stated purpose still gets a
                    # non-blank, deterministic one — the reject-if-absent rule holds on every path
                    # into `ConnectorJobInput`, and naming the step is more honest than inventing
                    # a reason nobody gave.
                    rationale=step.purpose or f"template step {step.id!r} (job {step.job})",
                    requested_by=identity.actor,
                    publish_to_graph=resolved.publish_to_graph,
                ),
                # Named from the run's *execution*, not just its id — `TemplateWorkflow` is also
                # launched with `ALLOW_DUPLICATE_FAILED_ONLY` (`templates/registry.py`), and a
                # step id alone made every already-completed step of the first execution refuse to
                # start on the second. See `child_workflow_id`.
                id=child_workflow_id(step.id),
                task_queue=settings.background_task_queue,
                result_type=ConnectorJobResult,
                # Still reject-duplicate: within one template execution two steps must never
                # collide on an id, which is a template-authoring bug worth failing loudly on.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                retry_policy=BAD_DATA_RETRY,
            ),
        )
