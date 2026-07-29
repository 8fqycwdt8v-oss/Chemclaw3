"""Running a template: the deterministic sequencer, and the reason a template is durable at all.

A template's whole value is that the order does not vary — so the thing that walks the steps must be
the one part of the system that is *replayable*. That is this workflow: it substitutes references
(pure), dispatches each step to an activity or a child workflow (I/O), accumulates results, and
pushes back to the launching session at the end.

**The resolved template travels in the input.** Not its name — the template itself. Editing
`templates/<name>.yaml` therefore cannot change a run already in flight, which is both the
versioning story (`templates/README.md`) and a hard replay requirement: a workflow that re-read a
file on replay would take a different path than its own history and Temporal would refuse it.
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
    from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobResult
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.template_activities import (
        AgentStepInput,
        StepIdentity,
        ToolStepInput,
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
@durable_workflow("background")
@workflow.defn
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
            return await self._run_job_step(step, scope, identity)
        raise ValueError(f"unknown template step kind {type(step).__name__}")

    async def _run_job_step(
        self, step: JobStep, scope: dict[str, Any], identity: StepIdentity
    ) -> ConnectorJobResult:
        """Run a connector job as a child workflow and await it — the whole point of a `job` step.

        A `tool` step naming a job launcher would return an id and move on, which is right in a chat
        turn (the agent must not block) and useless here: a template exists to sequence work, so it
        waits. Reusing `ConnectorJobWorkflow` rather than starting the connector's workflow directly
        keeps the job's cross-cutting concerns — the PR-gate publish, the actor attribution — in the
        one place that owns them.
        """
        # Imported inside the workflow's sandbox-passthrough at call time rather than at module
        # scope: the job lookup reads the connector registry, which does filesystem + YAML I/O on a
        # cold process (`discovered()` is `@cache`d, so once per worker) and is not something a
        # workflow module should pull in for a step kind most templates never use.
        with workflow.unsafe.imports_passed_through():
            from chemclaw.connectors.registry import find_job

        connector, job = find_job(step.job)
        # Addressed by type name, so the child's return is untyped at the call site; `result_type`
        # is what actually decodes it into a `ConnectorJobResult`.
        return cast(
            ConnectorJobResult,
            await workflow.execute_child_workflow(
                "ConnectorJobWorkflow",
                ConnectorJobInput(
                    connector=connector,
                    job=job.name,
                    workflow=job.workflow,
                    task_queue=job.task_queue,
                    payload=resolve(step.arguments, scope),
                    requested_by=identity.actor,
                    publish_to_graph=job.publish_to_graph,
                ),
                id=f"{workflow.info().workflow_id}-{step.id}",
                task_queue=settings.background_task_queue,
                result_type=ConnectorJobResult,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                retry_policy=BAD_DATA_RETRY,
            ),
        )
