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

import contextlib
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
        failure_reason,
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

from chemclaw.durable.publish import BAD_DATA_RETRY, agent_step_retry
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
            # The launching chat, carried down to every step rather than only to the push-backs.
            # It was already in this input and used only on the way *out*, so each step stamped no
            # ambient session and `agent/audit.py` booked `session_id=""` on every row a template
            # ever wrote — the trail could name the actor and the run but not the conversation the
            # run came from. Empty off the service path, exactly as it is here.
            session_id=run.session_id,
        )
        # **Every *declared* input is in scope, not only every supplied one.** `registry.py` dumps
        # the params with `exclude_none=True`, so an optional argument the caller omitted simply
        # was not there — and a template that references it unconditionally died on its first step
        # with `UnresolvedReference: 'inputs.solvent' ... have: ['inputs.smiles']`. Every template
        # in the tree references its optional `solvent` that way, `conformer-refinement` since the
        # day it shipped, so "run this without naming a solvent" — the gas-phase default, and the
        # commonest call there is — never worked for any of them.
        #
        # Seeding the declared names with `None` is the fix rather than editing eight YAML files,
        # because the templates are not wrong: an optional input that was not given *is* `None`,
        # which is precisely what the calc specs default to and what
        # `solvents.require_supported_solvents` reads as gas phase. A missing name still raises —
        # `manifest.py` already refuses a reference to an *undeclared* input at load time, so the
        # only thing this stops being an error is the one case that should never have been one.
        scope: dict[str, Any] = {f"inputs.{item.name}": None for item in run.template.inputs}
        scope.update({f"inputs.{key}": value for key, value in run.inputs.items()})
        results: dict[str, Any] = {}

        for step in run.template.steps:
            try:
                result = await self._run_step(step, scope, identity, timeout)
            except BaseException as exc:
                # The completion push-back below had no counterpart, so a template that failed at
                # step 3 of 5 told the chemist nothing at all: the workflow ended, the session
                # stream stayed silent, and the only record was in Temporal's history. The
                # connector-job workflow already answers this (`connector_job._notify_failure`) and
                # this is deliberately the same shape and the same best-effort stance — the run is
                # already failing, and a push-back that failed on top would replace one lost
                # message with two. Which step, because "the template failed" is unactionable when
                # a procedure has five of them.
                await self._notify_failure(run, step, exc)
                raise
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

    async def _notify_failure(self, run: TemplateRunInput, step: Any, exc: BaseException) -> None:
        """Tell the session which step failed, before the failure propagates and closes this run.

        Never raises, and that is not defensiveness: this runs on the way out of an already-failing
        workflow, so an exception here would replace the original failure with a push-back error and
        lose the reason entirely. `notify_session_best_effort` swallows its own transport failures;
        the guard is for everything else, including a `cancelled` teardown that reaches this line.
        """
        if not run.session_id:
            return
        with contextlib.suppress(Exception):
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

    async def _run_step(
        self, step: Any, scope: dict[str, Any], identity: StepIdentity, timeout: timedelta
    ) -> Any:
        """Dispatch one step on its kind, with its references already substituted."""
        # Both dispatched activities beat while they wait (`durable/heartbeat.beating`), so both
        # carry the timeout that beat is derived from. Without it `start_to_close_timeout` was the
        # only liveness signal a step had, and a worker killed mid-step was indistinguishable from
        # one still working: the whole per-step budget had to elapse before the attempt was retried,
        # so a pod eviction one minute into an `agent` step cost the run 15 idle minutes. It is
        # deliberately *not* on the `job` step below — that one is a local activity, and Temporal's
        # local activities do not heartbeat at all (`execute_local_activity` takes no
        # `heartbeat_timeout`); it is also a cached in-process lookup with nothing to wait on.
        heartbeat = timedelta(seconds=settings.template_step_heartbeat_timeout_seconds)
        if isinstance(step, ToolStep):
            return await workflow.execute_activity(
                run_tool_step,
                ToolStepInput(
                    tool=step.tool, arguments=resolve(step.arguments, scope), identity=identity
                ),
                start_to_close_timeout=timeout,
                heartbeat_timeout=heartbeat,
                retry_policy=BAD_DATA_RETRY,
            )
        if isinstance(step, AgentStep):
            # The one branch not on `BAD_DATA_RETRY`, and the difference is replay, not
            # classification. A retried tool step recomputes; a retried agent step re-runs the
            # whole turn from the prompt — an activity has no checkpointer — so every tool the
            # failed attempt already ran runs again with its side effects. Measured: one provider
            # 503 produced two PR-gate branches and two audit rows for one logical note. The
            # in-SDK retry (`llm_max_retries`) still absorbs a blip without any replay; see
            # `publish.agent_step_retry` for the outage this trades away.
            return await workflow.execute_activity(
                run_agent_step,
                AgentStepInput(
                    prompt=resolve(step.prompt, scope),
                    profile=step.profile,
                    # The step's declared writes travel with it, from the *pinned* template — so
                    # editing the file cannot widen a run already in flight, exactly as pinning the
                    # definition keeps an edit from changing its steps.
                    write_tools=step.write_tools,
                    identity=identity,
                    # Which step this is, so the turn it runs costs a `turn_costs` row of its own.
                    # That ledger is keyed on the correlation id and *upserts*
                    # (`agent/turn_cost_store.py`), and every step of a run shares the run's
                    # correlation id — so without this a two-`agent`-step template would book the
                    # second step's spend over the first's and report half of what it cost.
                    step_id=step.id,
                ),
                start_to_close_timeout=timeout,
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
        # this step used to start an expensive job with.
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
                # The same ceiling `ConnectorJobWorkflow` gives this child when it launches it
                # directly (`durable/connector_job.py`), because it is the same child doing the same
                # work — a template step must not be the one path on which a connector job runs
                # unbounded. `BAD_DATA_RETRY` bounds failures, not a job that simply never returns.
                execution_timeout=timedelta(seconds=settings.connector_job_timeout_seconds),
            ),
        )
