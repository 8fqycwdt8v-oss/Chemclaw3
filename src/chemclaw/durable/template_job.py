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
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import (
        ConnectorJobInput,
        ConnectorJobResult,
        child_workflow_id,
        failure_reason,
        wrapper_execution_timeout,
    )
    from chemclaw.durable.job_record import JobRecord, record_job
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
# The namespace a template run occupies in `job_records.connector`. Templates are not connectors and
# the column is not being repurposed: it is the *family* a row belongs to, which is what
# `find_past_jobs(connector=...)` filters on and what a reader needs to tell a `calc` run from a
# procedure. A literal rather than a setting, because it is an identifier inside stored rows —
# changing it would orphan every row already written.
TEMPLATE_JOB_FAMILY = "template"


def template_job_record(
    job_id: str, run: "TemplateRunInput", results: dict[str, Any], summary: str
) -> JobRecord:
    """The durable record of one finished template run.

    **Templates wrote no record at all**, and the gap was invisible because everything around it
    looked complete: a run pushed a completion event to its session, returned every step's result,
    and ended. But `record_job` had exactly one caller in the tree — `connector_job.py` — so a
    template run left no `job_records` row. It was therefore never findable by `find_past_jobs`,
    and `get_durable_job_status` answered for its id only until Temporal retained the history away.
    Nine shipped `run_*` procedures, one of them (`hazard-briefing`) whose entire product is a
    chemist-facing brief, and that brief was unrecoverable once the conversation closed.

    Pure and module-level for the same reason `job_record_for` is: everything around it needs a
    live broker, and this way "a template run records what it ran and what every step produced" is
    a property the offline suite can hold rather than one only CI ever checks.

    `rationale` is deliberately empty — see `JobRecord.rationale`. This row's `job` column names a
    reviewed `data/templates/<name>.yaml` whose own `summary` states what the procedure is for, so
    copying that here would restate another store's fact in a field documented as the requester's
    own words.
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
        # Every step, not only the last. A fixed procedure's value is being able to show what each
        # stage produced, which is why `TemplateRunResult` keeps them; reconstructing them from
        # Temporal history afterwards is not something a chemist can do.
        result={"steps": results},
        payload_kind="template",
    )


def failed_template_record(
    job_id: str, run: "TemplateRunInput", step_id: str, reason: str, completed: dict[str, Any]
) -> JobRecord:
    """The record of a template run that ended badly — what was asked, where it stopped, and why.

    The counterpart `connector_job.failed_job_record` already argues for: a run that fails is
    exactly the run somebody goes looking for months later, and it was the one leaving nothing.
    `summary` stays empty because a summary is what a run *produced*; the reason goes in
    `failure_reason`, so a listing can tell a result from a failure without opening either.

    The steps that *did* complete are kept. A five-step procedure that died at step four ran four
    real steps, and discarding them would lose the work while recording only the failure.
    """
    return JobRecord(
        job_id=job_id,
        connector=TEMPLATE_JOB_FAMILY,
        job=run.template.name,
        requested_by=run.requested_by,
        session_id=run.session_id,
        correlation_id=job_id,
        payload=dict(run.inputs),
        result={"steps": completed},
        payload_kind="template",
        state="failed",
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
                # The record comes first for the same reason it does on the success path, and
                # matters more here: a run that failed is the one somebody goes looking for, and
                # until now it left nothing anywhere but Temporal's expiring history.
                await self._record_run(
                    failed_template_record(
                        workflow.info().workflow_id,
                        run,
                        step.id,
                        failure_reason(exc),
                        dict(results),
                    )
                )
                await self._notify_failure(run, step, exc)
                raise
            results[step.id] = result
            scope[f"steps.{step.id}.result"] = result

        summary = f"template {run.template.name!r} completed {len(run.template.steps)} step(s)"
        # Recorded before the push-back, so the id a chemist is handed is one `find_past_jobs` and
        # `get_durable_job_status` can already answer for. Best-effort in the same sense the
        # connector wrapper means it: a finished run is finished, and losing its row must not undo
        # the work or fail the workflow.
        await self._record_run(
            template_job_record(workflow.info().workflow_id, run, results, summary)
        )
        if run.session_id:
            await notify_session_best_effort(
                run.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "template": run.template.name,
                    "summary": summary,
                },
            )
        # The last step's result is the run's answer: a procedure ends with the thing it was for,
        # and a caller that wants an earlier stage has every one of them in `steps`.
        last = run.template.steps[-1].id
        return TemplateRunResult(template=run.template.name, steps=results, result=results[last])

    async def _record_run(self, record: JobRecord) -> None:
        """Persist the run's durable record, logging rather than failing the run if it cannot be.

        A method rather than an inline block so "never fail a finished run for the sake of its
        record" has one place to be read and one to change — the same shape, and the same
        reasoning, as `connector_job.ConnectorJobWorkflow._record_run`.

        The queue is named explicitly although this workflow already runs there: `record_job` is
        registered on the background queue alone, so were the template wrapper ever moved, the
        default would route the write to a queue nothing serves — a silent loss, discovered when an
        id expires months later.
        """
        try:
            await workflow.execute_activity(
                record_job,
                record,
                task_queue=settings.background_task_queue,
                start_to_close_timeout=timedelta(seconds=settings.job_record_timeout_seconds),
                # **`start_to_close` alone is not a bound on this call.** It starts counting when a
                # worker picks the task up, so an unserved background queue — a fleet scaled to
                # zero, a rolling update, a queue named in config and served by no pod — is a
                # template run that never ends. `tests/test_activity_queue_bound.py` holds that
                # rule over every call site in `durable/` and caught this one: without the bound
                # below, the first version of this method hung the whole suite rather than failing
                # it, which is the same wedge in miniature.
                #
                # **`light_write_queue_wait_timeout()`, the same bound `connector_job._record_run`
                # and `durable/notify.py` pass, and for the same measured reason.** This was a
                # `schedule_to_close_timeout` at twice the work budget — 60 s — on the claim that
                # it matched the connector wrapper "exactly"; it stopped matching when that pair
                # was fixed and this third call site was missed. Schedule-to-close is a *total*,
                # spent almost entirely on a queue this call does not control: `background-jobs`
                # also carries 900 s template agent steps and the hourly sweeps across eight slots,
                # measured at 41.6 s of queueing for a 50 ms activity and ~150 s expected at target
                # load, so the row was simply lost — and it capped all five attempts together,
                # deleting the retry budget as well. Splitting the two quantities is what these
                # timeouts are for: the bound above is the work, this one is the wait, and it is
                # the *light* wait rather than core's hour because it sits at the end of a run.
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
                schedule_to_start_timeout=queue_wait_timeout(),
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
                    # **The two ids the template path dropped, and the reason its failures were
                    # completely silent.** `StepIdentity` has carried both since it was written —
                    # `session_id` from `TemplateRunInput` and `correlation_id` set to this run's
                    # own workflow id — and this call built a `ConnectorJobInput` without either.
                    # `ConnectorJobWorkflow._notify_failure` short-circuits on `if not
                    # job.session_id: return`, so a connector job that failed inside a template
                    # told the launching chat nothing; combined with the failure record it also
                    # wrote no row and moved no metric, leaving the run in Temporal's expiring
                    # history alone. Passing them through is the whole fix: every obligation the
                    # wrapper carries keys off one of these two fields.
                    session_id=identity.session_id,
                    correlation_id=identity.correlation_id,
                    # The job's own declared ceiling, so a job launched from a template is bounded
                    # exactly as the same job launched from a chat turn is — a field this path
                    # drops is a field that quietly means something else here.
                    timeout_seconds=resolved.timeout_seconds,
                    # Its sibling, and the third field this literal has silently defaulted. A job
                    # declaring `awaits_answer` gets no child ceiling (`child_execution_timeout`)
                    # because it spends wall clock waiting on a plate rather than computing;
                    # dropped here it read False, so the shipped campaign job was handed the
                    # five-hour fleet ceiling on this path and killed 67x short of the fourteen-day
                    # deadline its own wait opens. What still bounds it here is the *wrapper's*
                    # `execution_timeout` below, which is a step-level bound and a different
                    # question — see `wrapper_execution_timeout`.
                    awaits_answer=resolved.awaits_answer,
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
                # **One attempt**, the same bound `ConnectorJobWorkflow._run_child` gives this
                # child when it starts it directly, and for the same measured reason: Temporal
                # matches `non_retryable_error_types` against the *outermost* failure, so a child
                # that failed through its own activity surfaces as a child/activity failure — a
                # name deliberately absent from `_BAD_DATA_TYPES` — and `BAD_DATA_RETRY` at a
                # child-workflow boundary therefore classifies nothing and degrades to a plain
                # `maximum_attempts=5`. Measured against a live broker: one deterministic
                # `ValueError` cost **5 full connector-job executions**, each minting a fresh
                # grandchild id (`child_workflow_id` is keyed on the parent's run id), so the
                # bundle workflow and its calculation ran from scratch every time — and the D-011
                # cache cannot help, because a failed run stores nothing. Nothing is lost: the
                # child's own activities already carry `BAD_DATA_RETRY`, where the classification
                # works, and a worker that dies mid-child is re-delivered without a workflow retry.
                retry_policy=RetryPolicy(maximum_attempts=1),
                # A template step must not be the one path on which a connector job runs
                # unbounded — `BAD_DATA_RETRY` bounds failures, not a job that simply never
                # returns — but the ceiling must be the *wrapper's*, not the child's. This used to
                # be `connector_job_timeout_seconds`, the identical number `ConnectorJobWorkflow`
                # then gives its own child, which leaves zero headroom: the wrapper starts first,
                # so its ceiling expires first, and an execution timeout is not delivered to
                # workflow code — the `except BaseException -> _notify_failure` clause never runs
                # and the run ends TIMED_OUT with no push-back and no `job_records` row. See
                # `wrapper_execution_timeout`, which owns the relation beside the child ceiling it
                # has to clear.
                execution_timeout=wrapper_execution_timeout(),
            ),
        )
