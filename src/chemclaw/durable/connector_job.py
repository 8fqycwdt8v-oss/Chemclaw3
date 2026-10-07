"""The one durable wrapper every connector job runs inside — core keeps the cross-cutting concerns.

The connector owns its workflow code and worker; this wrapper owns what must never vary per
capability:

- **Idempotency** — the id is derived from the job and its arguments by
  `chemclaw.connectors.jobs`, with `ALLOW_DUPLICATE_FAILED_ONLY`, so re-asking joins the existing
  run and only a failed one re-executes (D-011).
- **Attribution** — the requesting actor travels in the payload and is handed to the child on its
  memo, so a backend under a shared service identity can name the user without the actor being a
  model-authored field.
- **The write path** — a job returns a `Note` and core writes it through `chemclaw.kg.record`; a
  connector never writes to the graph itself (`tests/test_knowledge.py`).
- **Session push-back** — the launching chat is woken through the one existing channel.
- **The durable record** — what ran, on what, what came out and why is written to `job_records`
  (D-157), since Temporal expires a closed run's history.

The child is addressed by workflow type name (`JobSpec.workflow`) and a task queue derived from
the connector's name (`connectors/queues.py::bundle_queue`), so this module imports nothing from
any connector.
"""

import contextlib
import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    is_cancelled_exception,
)
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.refusal_route import sentence_of
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import degraded
    from chemclaw.durable.awaiting import AwaitAnswerWorkflow, AwaitOutcome, AwaitRequest
    from chemclaw.durable.deliver_message import OutboundMessage, deliver_best_effort
    from chemclaw.durable.effect_ledger import EffectRecord, begin_effect, settle_effect
    from chemclaw.durable.job_record import JobRecord, note_with_run_provenance, record_job
    from chemclaw.durable.memory_jobs import publish_memory_note_activity
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish_results import JobPublishInput, publish_job_result
    from chemclaw.kg.note import Note
    from chemclaw.memory.jobs import SynthesisUnit

from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    activity_failure_reason,
    light_write_queue_wait_timeout,
    publish_note_best_effort,
    publish_result_best_effort,
    queue_wait_timeout,
)
from chemclaw.durable.registry import durable_activity, durable_workflow

# A plain module logger, used only inside an `is_replaying` guard that also covers the metric
# beside it, so the count and the line describe the same event.
logger = logging.getLogger(__name__)


# How much of a failure's own sentence is kept; the same cap `publish_results.py` applies.
_REASON_MAX_CHARS = 500


def ended_state(exc: BaseException) -> str:
    """How a run that did not complete ended, as `job_records.state` spells it.

    `cancelled` when the exception is a cancellation (`is_cancelled_exception`, the SDK's
    predicate),
    `failed` otherwise. A cancellation is a decision, not a defect, so it is stored as one, matches
    `GET /jobs/{id}`, and is not counted as a failure.
    """
    return "cancelled" if is_cancelled_exception(exc) else "failed"


def failure_reason(exc: BaseException) -> str:
    """The application's own account of why a job failed, for a human to read.

    Shared by this wrapper and `connectors.jobs` so one failure has one reason. Temporal nests
    structurally (`ChildWorkflowError` wraps `ActivityError` wraps what the code raised), so the
    structural frames are skipped and the *first* application-level message is taken — not the
    innermost, which is usually a library's internals rather than the sentence written for the
    user. A client-side `WorkflowFailureError` is stripped by the caller, keeping the client package
    out of the workflow sandbox.

    Bounded to `_REASON_MAX_CHARS`, because the string is stored, hashed into a dedupe key and shown
    in a model turn. A refusal's routing footer (`agent/refusal_route.routed`, written for the
    model) is stripped, since this string's readers are people.
    """
    cause: BaseException = exc
    while isinstance(cause, (ChildWorkflowError, ActivityError)) and cause.__cause__ is not None:
        cause = cause.__cause__
    return sentence_of(str(cause) or type(cause).__name__)[:_REASON_MAX_CHARS]


class ConnectorJobInput(BaseModel):
    """What core needs to run one connector job: where the work lives, and the turn it came from.

    `workflow` comes straight from the manifest's `JobSpec` and `task_queue` is derived from
    `connector` at dispatch (`bundle_queue`, D-150); together they are the *only* thing binding
    this run to a connector — no import, no shared type. `payload` is the model-supplied
    arguments already validated against the job's generated params model, so the child receives a
    plain, replay-stable mapping rather than a type core would have to know.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str = Field(min_length=1)
    job: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    task_queue: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Why this run was asked for, in the requester's own terms (D-157). Required, and kept out of
    # `payload` so differently-worded reasons do not split one idempotency key into two expensive
    # runs. No other store records what question a run was meant to answer.
    rationale: str = Field(min_length=1)
    # The Entra actor this run is attributed to (`require_actor` guarantees it under Entra). Carried
    # in the payload because a workflow has no request context.
    requested_by: str = Field(min_length=1)
    # The chat to wake on completion; empty off the service path (CLI, tests), where there is no
    # session to push back to.
    session_id: str = ""
    # The turn that launched this run, so the durable execution joins the conversation's audit
    # trail.
    # Empty off the request path.
    correlation_id: str = ""
    # The plan step this run was launched for (the first `in_progress` todo) and its plan revision.
    # Read ambiently at the launch site, never model-authored; empty when not launched from a plan.
    # Defaulted because the field crosses the Temporal wire.
    plan_step: str = ""
    plan_hash: str = ""
    publish_to_graph: bool = False
    # The job's *declared* ceiling (`JobSpec.timeout_seconds`), copied from the manifest at launch
    # because a workflow may not read it. `None` means none declared. Carried unresolved so the
    # `min`
    # against the deployment setting is applied by `child_execution_timeout` when the child starts,
    # and a lowered setting binds still-queued jobs. Defaulted for the wire.
    timeout_seconds: float | None = Field(default=None, gt=0)
    # What this job changes in a system this deployment does not own, copied from the manifest at
    # launch. Empty means the job's writes are this system's own. Defaulted for the wire.
    effect_system: str = ""
    effect_reversal: str = ""
    # Who may approve an irreversible effect, resolved from configuration at launch (a workflow may
    # not read `settings`). Empty means no approver role, which `_approve_effect` refuses.
    effect_approver: str = ""
    # How long that approval stays open, already clamped at launch so no timer count depends on
    # `settings` during replay.
    effect_approval_days: float = 3.0
    # Whether this job suspends on a person (`JobSpec.awaits_answer`), copied from the manifest at
    # launch. `True` means elapsed time is not cost; `child_execution_timeout` is where this and
    # `timeout_seconds` meet. Defaulted for the wire.
    awaits_answer: bool = False


class ConnectorJobResult(BaseModel):
    """The result envelope every connector workflow returns — the whole cross-process contract.

    `summary` is the one line the chat shows and the model reads; `data` is the job's own structured
    result, opaque to core (a connector's domain types stay the connector's business); `note` is the
    optional knowledge contribution. Typing `note` as the existing frozen `Note` means a connector's
    contribution passes the graph's own slug and schema validators on the way in, so a malformed
    note is rejected at the boundary instead of failing later inside the note write.

    **`extra="ignore"`, and the asymmetry with `ConnectorJobInput` above is the decision.** Five
    fields on this wire say in as many words that they are "additive and defaulted because it
    crosses the Temporal wire and histories are in flight". That rule made **core to bundle**
    additions safe and left the return direction closed: the wrapper runs in core's image and the
    child in the bundle's, so during a rolling upgrade the bundle is routinely the *newer* of the
    two, and one field it has learned to emit was rejected by the older core — killing every
    in-flight job of that bundle. Measured before this changed, the death was silent: no
    `job_records` row, no push-back. An unknown field on a *result* is a separately-deployed image's
    business and this core has no use for it; an unknown field on `ConnectorJobInput` is core
    writing to itself, where every launch site is in this repository and an extra is a bug that must
    fail loudly. `test_the_launch_side_of_the_wire_still_refuses_what_it_does_not_know` holds the
    pair, because nothing else states which posture belongs on which end.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    note: Note | None = None
    # The far side's own handle for what this run changed (ticket number, deviation id); recorded on
    # the effect ledger as the handle an operator can undo by hand. Empty for jobs that change
    # nothing
    # outside this deployment. Only the connector that made the change knows it.
    external_ref: str = ""
    # The calculation keys this run rested on, so a conclusion drawn from it can cite them and a
    # stale
    # calculation can be traced to its conclusions. Defaulted for the wire; empty means "recorded
    # none", never "used none".
    calc_refs: list[str] = Field(default_factory=list)
    # The name of the pydantic model `data` was dumped from. `chemclaw.publish` dispatches on it
    # (`PAYLOAD_PROJECTORS`); a composite has no `calc_type` to infer a projector from, so without
    # this it would be dropped. Set from `type(result).__name__` where the typed result is still
    # held.
    # Defaulted for the wire; empty means "infer".
    payload_kind: str = ""


def envelope_from_result(job_id: str, raw: Any) -> ConnectorJobResult:
    """Decode what a finished durable job returned, or say why it is not a job of ours.

    The one decoder for every collector of a finished job's result. Raises a written `ValueError`
    rather than letting pydantic's `ValidationError` (also a `ValueError`, passed through to the
    user) relay a field dump.

    Args:
        job_id: The workflow id the result belongs to, for the message.
        raw: Whatever the workflow returned, undecoded.

    Raises:
        ValueError: When the result is not the connector envelope.
    """
    try:
        return ConnectorJobResult.model_validate(raw)
    except ValidationError as exc:
        # Hard, not a degraded status: every launcher produces the envelope, so this is a foreign
        # workflow id, and "completed" with no result would withhold the answer.
        raise ValueError(
            f"durable job {job_id!r} completed but did not return the connector job envelope; "
            "the id does not belong to a job any launcher in this system started"
        ) from exc


def child_workflow_id(suffix: str) -> str:
    """The id for a child of the *current* execution: parent id, parent run id, then `suffix`.

    Parents run under a deterministic id with `ALLOW_DUPLICATE_FAILED_ONLY` so a failed run can
    re-execute; including the run id gives the re-execution fresh child ids, which
    `REJECT_DUPLICATE` would otherwise refuse. `REJECT_DUPLICATE` stays: one child per parent
    execution is the invariant. `run_id` is replay-stable within a run and differs across runs.
    """
    info = workflow.info()
    return f"{info.workflow_id}-{info.run_id}-{suffix}"


def job_record_for(
    job_id: str,
    job: ConnectorJobInput,
    result: ConnectorJobResult,
    runtime_seconds: float = 0.0,
) -> JobRecord:
    """Assemble the durable record of one finished run from its input and its result (D-157).

    Module-level and pure so the offline suite can test it without a Temporal server.
    """
    return JobRecord(
        job_id=job_id,
        connector=job.connector,
        job=job.job,
        rationale=job.rationale,
        requested_by=job.requested_by,
        session_id=job.session_id,
        correlation_id=job.correlation_id,
        plan_step=job.plan_step,
        plan_hash=job.plan_hash,
        payload=job.payload,
        summary=result.summary,
        # The envelope's own data, whole: for a campaign that is every observation it made, which
        # is the part Temporal's expiring history was the only copy of.
        result=result.data,
        note_id=result.note.id if result.note is not None else "",
        calc_refs=result.calc_refs,
        runtime_seconds=runtime_seconds,
        payload_kind=result.payload_kind,
    )


def failed_job_record(
    job_id: str,
    job: ConnectorJobInput,
    reason: str,
    runtime_seconds: float,
    *,
    state: str = "failed",
) -> JobRecord:
    """The durable record of a run that ended badly — what was asked for, and why it broke.

    A failed run still gets a row, so "what did we try and why" has an answer for it. Separate from
    `job_record_for` because there is no envelope (`ConnectorJobResult.summary` is required).
    `summary`
    stays empty and the reason goes in `failure_reason`, so a listing can tell a result from a
    failure. The caller sets `state` from `ended_state(exc)`.
    """
    return JobRecord(
        job_id=job_id,
        connector=job.connector,
        job=job.job,
        rationale=job.rationale,
        requested_by=job.requested_by,
        session_id=job.session_id,
        correlation_id=job.correlation_id,
        plan_step=job.plan_step,
        plan_hash=job.plan_hash,
        payload=job.payload,
        runtime_seconds=runtime_seconds,
        state=state,
        failure_reason=reason,
    )


# The wrapper only starts a child on the connector's queue and waits, so it runs on the light
# background queue; the capability is heavy, the wrapper is not (D-006).


def finish_headroom() -> timedelta:
    """What the wrapper may still spend *after* its child returns, from the steps' own budgets.

    Six steps follow `_run_child`: settle the effect ledger, write the durable record, offer the
    composite to the results store, write the note, push back to the session, and send the
    `job-result` copy. Each may take its own `schedule_to_start` plus `start_to_close`, read from
    the
    call sites' helpers so a moved bound moves this. Without enough headroom a job whose child hit
    its ceiling is reaped `TIMED_OUT` before recording its failure or telling the chemist. One
    attempt each, matching every other ceiling here.

    Returns:
        The wall clock the wrapper's post-child steps may spend between them.
    """
    queue = queue_wait_timeout()
    light = light_write_queue_wait_timeout()
    activity_budget = timedelta(seconds=settings.activity_timeout_seconds)
    return (
        # `_settle_effect`, on either ending.
        queue
        + activity_budget
        # `_record_run`.
        + light
        + timedelta(seconds=settings.job_record_timeout_seconds)
        # `_publish_result`.
        + queue
        + timedelta(seconds=settings.result_publish_timeout_seconds)
        # The note write.
        + queue
        + timedelta(seconds=settings.note_write_timeout_seconds)
        # The session push-back, on either ending.
        + light
        + activity_budget
        # The outbound `job-result` copy, on either ending: the light queue wait plus
        # `delivery_timeout_seconds`. Reserved even with delivery off, since it is what the step may
        # spend.
        + light
        + timedelta(seconds=settings.delivery_timeout_seconds)
    )


def wrapper_execution_timeout() -> timedelta:
    """A ceiling for the *wrapper*, strictly above the one it hands its own child.

    `connector_job_timeout_seconds` plus `finish_headroom`. A wrapper bounded at exactly the child's
    ceiling expires first, and an execution timeout never reaches the failure clause, so the run
    would end with no push-back and no record. The direct path (`connectors/jobs.py`) sets no
    wrapper
    timeout; this is for the template path.

    Always the global number, even for a job that lowered its own ceiling: a declared ceiling can
    only lower the child's, so the global value clears all of them. A job declaring `awaits_answer`
    gets no child ceiling, so on the template path this is its only bound and a long campaign (or an
    effect approval inside the wrapper) is cut off here; raising it is an operator decision about
    `connector_job_timeout_seconds` and `template_run_timeout_seconds`.
    """
    return timedelta(seconds=settings.connector_job_timeout_seconds) + finish_headroom()


def child_execution_timeout(
    declared: float | None, awaits_answer: bool = False
) -> timedelta | None:
    """The ceiling one connector job's child actually gets — or none, where wall clock is not cost.

    The deployment sets the maximum (`connector_job_timeout_seconds`); a bundle may declare less
    (`JobSpec.timeout_seconds`), so the minimum is taken and a manifest can never grant itself more
    runtime than the operator funded. `None` returns the setting unchanged.

    A job that suspends on a person (`awaits_answer`) gets no ceiling: its wall clock is waiting,
    not work, and no finite ceiling fits a multi-round campaign. It stays bounded otherwise: each
    wait is clamped at `awaiting_max_days`, an unanswered wait ends the campaign, every activity
    has its own timeouts, and rounds are capped by `bo_max_rounds`. Module-level so the offline
    suite can test it.

    Args:
        declared: The job's own ceiling in seconds, or `None` where its manifest declared none.
        awaits_answer: Whether the job suspends on a durable answer (`JobSpec.awaits_answer`).

    Returns:
        The execution timeout to hand the child workflow, or `None` to leave it unbounded.
    """
    if awaits_answer:
        return None
    ceiling = settings.connector_job_timeout_seconds
    return timedelta(seconds=ceiling if declared is None else min(declared, ceiling))


@durable_workflow("background")
# `failure_exception_types` so this workflow can fail rather than hang: the SDK otherwise parks a
# plain exception (e.g. `envelope_from_result`'s `ValueError`) in an endless workflow-task retry
# loop, and the parent has no execution timeout, so the chemist would see "running" forever. The
# trade: a code bug in a redeploy fails in-flight jobs instead of parking them.
@workflow.defn(failure_exception_types=[Exception])
class ConnectorJobWorkflow:
    """Run one connector-owned workflow as a child, then publish and notify on its behalf."""

    def __init__(self) -> None:
        """Start with no durable record written for this execution.

        `_recorded` lets the failure path ask whether `_finish` already wrote the completed record
        before a later best-effort step raised. Per-execution instance state, deterministic under
        replay.
        This workflow is not yet covered by `tests/test_workflow_replay.py`
        (`UNCOVERED_BACKGROUND_WORKFLOWS`); the suite holds the effect instead
        (`test_a_run_that_fails_after_recording_is_not_recorded_a_second_time`).
        """
        self._recorded = False

    @workflow.run
    async def run(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Execute the connector's workflow, write any note it produced, and wake its session.

        The child runs on the connector's own task queue. A child failure propagates (the job really
        failed), unlike the best-effort note publish and push-back, which run after the result is
        durable. A failure is pushed back to the session before it propagates, because the chemist
        was
        already told the job is running and silence would read as success.
        """
        # `workflow.now()` and not `time.monotonic()`: a workflow's clock must come from the one
        # Temporal records in history, or a replay would measure the replay rather than the run.
        started_at = workflow.now()
        job_id = workflow.info().workflow_id
        try:
            approved_by = await self._approve_effect(job, job_id)
            await self._begin_effect(job, job_id, approved_by)
            result = await self._run_child(job)
            await self._settle_effect(
                job, job_id, "applied", result.summary, external_ref=result.external_ref
            )
            return await self._finish(job, result, started_at)
        except BaseException as exc:
            # An eviction (terminate, cache eviction, worker shutdown) closes the coroutine from
            # outside the
            # workflow event loop; nothing below can run there, and the run is not lost (another
            # worker
            # replays it), so leave.
            if not workflow.in_workflow():
                raise
            # Settle the ledger first: a failed effect left `attempting` would read as "may have
            # changed the
            # far side".
            await self._settle_effect(job, job_id, "failed", str(exc)[:500])
            # Covers every way the run can end badly, not only a failing child. `BaseException` so a
            # cancellation is announced too; `_notify_failure` never raises, so it cannot replace
            # the real
            # reason.
            #
            # The failure record is written before the push-back (the durable copy first), and only
            # when no
            # completed record stands: a later best-effort step can raise after `_finish` recorded
            # the
            # science, and a second record would double-count the run. `job_record_store` separately
            # never
            # lets a failure write erase a stored result.
            if not self._recorded:
                await self._record_run(
                    failed_job_record(
                        job_id,
                        job,
                        failure_reason(exc),
                        (workflow.now() - started_at).total_seconds(),
                        state=ended_state(exc),
                    )
                )
            await self._notify_failure(job, exc)
            raise

    async def _approve_effect(self, job: ConnectorJobInput, job_id: str) -> str:
        """For an irreversible effect, suspend until a human approves *this call*.

        Per call rather than per plan: an approved plan authorises a kind of work, not this
        particular
        act. A refusal or an expiry fails the job; an unanswered approval is not an approval.
        """
        if job.effect_reversal != "irreversible":
            return ""
        # Fail closed on an unrouted approval, in every environment: an empty `asked_of` would let
        # any
        # caller, including the requester, approve an irreversible change.
        if not job.effect_approver:
            raise ApplicationError(
                f"{job.job!r} changes {job.effect_system} irreversibly and no approver role is "
                "configured (CHEMCLAW_EFFECT_APPROVAL_ROLE); nothing was attempted",
                non_retryable=True,
            )
        outcome = AwaitOutcome.model_validate(
            await workflow.execute_child_workflow(
                AwaitAnswerWorkflow.run,
                AwaitRequest(
                    kind="approval",
                    subject=f"Approve {job.job!r} against {job.effect_system}",
                    rationale=job.rationale,
                    requested_by=job.requested_by,
                    session_id=job.session_id,
                    correlation_id=job.correlation_id,
                    # Routed, so the answer gate has something to check. Unrouted, this reached
                    # `_may_answer`'s "anybody" branch and the requester could approve themselves.
                    asked_of=job.effect_approver,
                    deadline_days=job.effect_approval_days,
                ).model_dump(mode="json"),
                id=f"{job_id}:approval",
                task_queue=settings.background_task_queue,
                # Not the default `TERMINATE`, which never resumes workflow code and would leave the
                # approval's
                # `pending_requests` row `waiting` forever. Not `ABANDON`, which would leave a live
                # question for a
                # dead job. `REQUEST_CANCEL` delivers the cancellation the wait handles by settling
                # its row.
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
        )
        if outcome.state != "answered" or not outcome.payload.get("approved", False):
            raise ApplicationError(
                f"{job.job!r} changes {job.effect_system} irreversibly and was not approved "
                f"({outcome.state}); nothing was attempted",
                non_retryable=True,
            )
        return outcome.answered_by

    async def _begin_effect(self, job: ConnectorJobInput, job_id: str, approved_by: str) -> None:
        """Record the intent to change something outside, *before* attempting it."""
        if not job.effect_system:
            return
        await workflow.execute_activity(
            record_effect_activity,
            EffectRecord(
                effect_id=job_id,
                connector=job.connector,
                job=job.job,
                system=job.effect_system,
                reversal=job.effect_reversal or "idempotent",
                requested_by=job.requested_by,
                session_id=job.session_id,
                correlation_id=job.correlation_id,
                approved_by=approved_by,
            ),
            start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )

    async def _settle_effect(
        self,
        job: ConnectorJobInput,
        job_id: str,
        state: str,
        detail: str,
        external_ref: str = "",
    ) -> None:
        """Record how the attempt ended. Never raises: the ledger must not fail the job."""
        if not job.effect_system:
            return
        try:
            await workflow.execute_activity(
                settle_effect_activity,
                SettleEffectInput(
                    effect_id=job_id, state=state, detail=detail, external_ref=external_ref
                ),
                start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
        except Exception:
            # An unsettled row is the safe failure (state in doubt); raising would fail a job whose
            # work
            # succeeded.
            workflow.logger.warning("effect ledger not settled for %s", job_id)

    async def _run_child(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Start the bundle's own workflow on its queue, wait for its result, and decode it here.

        The result is taken untyped and decoded with `envelope_from_result` inside workflow code:
        the
        SDK's `result_type` decode runs outside the coroutine, so a bad payload would fail the run
        before
        the failure clause could record it and tell the chemist.
        """
        raw = await workflow.execute_child_workflow(
            job.workflow,
            job.payload,
            id=child_workflow_id("run"),
            task_queue=job.task_queue,
            # Actor, correlation id and session id ride on the memo, not in the model-authored
            # `payload`, so
            # none of them can be written by the LLM. Bundles read them with `workflow.memo_value`
            # (e.g.
            # `BoCampaignWorkflow` uses the session to address its durable waits).
            memo={
                "requested_by": job.requested_by,
                "correlation_id": job.correlation_id,
                "session_id": job.session_id,
            },
            # One child per parent execution (see `child_workflow_id`), so a duplicate id is a bug.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            # One attempt: `BAD_DATA_RETRY` cannot classify a child failure (Temporal matches the
            # outermost
            # failure name), so retries here would only duplicate compute. The child's activities
            # already
            # retry transients, and a dead worker is redelivered without a workflow retry.
            retry_policy=RetryPolicy(maximum_attempts=1),
            # See `child_execution_timeout`.
            execution_timeout=child_execution_timeout(job.timeout_seconds, job.awaits_answer),
        )
        return envelope_from_result(workflow.info().workflow_id, raw)

    async def _publish_result(self, job: ConnectorJobInput, result: ConnectorJobResult) -> None:
        """Offer this run's own result to the external results store, if one is configured.

        The envelope's `data` is a composite with no `calculation_results` row, so this is its only
        path to a results store. `calc_ref` is the deterministic workflow id (a composite's identity
        is
        the run), which makes it idempotent. Runs through an activity because a workflow may not
        touch a
        database.
        """
        if not result.data:
            return
        job_id = workflow.info().workflow_id
        await publish_result_best_effort(
            publish_job_result,
            [
                JobPublishInput(
                    calc_ref=job_id,
                    calc_type=f"{job.connector}.{job.job}",
                    payload_kind=result.payload_kind,
                    payload=result.data,
                    depends_on=list(result.calc_refs),
                    actor=job.requested_by,
                    session_id=job.session_id,
                    correlation_id=job.correlation_id,
                    job_id=job_id,
                    rationale=job.rationale,
                    # Same expression as `job_records.note_id`, from the same envelope.
                    note_id=result.note.id if result.note is not None else "",
                )
            ],
            label=f"{job.connector}:{job.job}",
        )

    async def _notify_failure(self, job: ConnectorJobInput, exc: BaseException) -> None:
        """Tell the session its job failed, before the failure propagates and closes this run.

        Best-effort and never raising: the `suppress(BaseException)` covers what
        `notify_session_best_effort` does not swallow (a `ValidationError`, a cancellation), so the
        caller's `raise` keeps the original failure.
        """
        reason = failure_reason(exc)
        if job.session_id:
            with contextlib.suppress(BaseException):
                await notify_session_best_effort(
                    job.session_id,
                    "job_failed",
                    {
                        "job_id": workflow.info().workflow_id,
                        "connector": job.connector,
                        "job": job.job,
                        "reason": reason,
                    },
                )
        # The outbound `job-result` copy on failure as well as success, since a run with no session
        # (Schedule- or inbox-started) has no other way to report it failed.
        with contextlib.suppress(BaseException):
            await deliver_best_effort(
                OutboundMessage(
                    recipient=job.requested_by,
                    subject=f"{job.connector}:{job.job} failed",
                    body=reason,
                    kind="job-result",
                    correlation_id=job.correlation_id,
                )
            )

    async def _finish(
        self, job: ConnectorJobInput, result: ConnectorJobResult, started_at: datetime
    ) -> ConnectorJobResult:
        """Record the run, write its note into the graph, and push the completion back."""
        record = job_record_for(
            workflow.info().workflow_id,
            job,
            result,
            runtime_seconds=(workflow.now() - started_at).total_seconds(),
        )
        # Written before the note publish: this row is the durable copy, while the graph write is
        # best-effort. Best-effort itself so a down database cannot fail a finished job, but logged
        # at
        # error because it loses data nothing else holds. The return value tells the failure clause
        # not
        # to record the run a second time.
        self._recorded = await self._record_run(record)
        # The external results store, if configured; best-effort like its neighbours. This is the
        # hook
        # that reaches composites, which have no cache row of their own.
        await self._publish_result(job, result)
        if job.publish_to_graph and result.note is not None:
            # The same note-write activity the memory-synthesis jobs use, stamped with the run and
            # its reason
            # here so no bundle can forget. `job.requested_by` attributes the write, so its log
            # lines name
            # the chemist.
            await publish_note_best_effort(
                publish_memory_note_activity,
                [
                    # A connector job never retires anything, so its unit carries the note alone.
                    # `ran_on` is
                    # `workflow.now()` (replay-safe) and dates an undated note so digests see it.
                    SynthesisUnit(
                        note=note_with_run_provenance(
                            result.note, record, ran_on=workflow.now().date()
                        )
                    ),
                    job.requested_by,
                ],
                label=f"{job.connector}:{job.job}",
            )
        if job.session_id:
            await notify_session_best_effort(
                job.session_id,
                "job_completed",
                {
                    "job_id": workflow.info().workflow_id,
                    "connector": job.connector,
                    "job": job.job,
                    "summary": result.summary,
                },
            )
        # Out of the building, addressed to whoever launched it (`requested_by` is always set). Last
        # and
        # best-effort: everything durable is already written.
        await deliver_best_effort(
            OutboundMessage(
                recipient=job.requested_by,
                subject=f"{job.connector}:{job.job} finished",
                body=result.summary,
                kind="job-result",
                correlation_id=job.correlation_id,
            )
        )
        return result

    async def _record_run(self, record: JobRecord) -> bool:
        """Persist the run's durable record, logging rather than failing the job if it cannot be.

        Returns whether the record was written; the success path uses it so a later failure is not
        recorded twice.
        """
        try:
            await workflow.execute_activity(
                record_job,
                record,
                # Named explicitly: the activity is registered only on the background queue.
                task_queue=settings.background_task_queue,
                start_to_close_timeout=timedelta(seconds=settings.job_record_timeout_seconds),
                # `start_to_close` bounds only the work; this bounds the wait on a busy or unserved
                # background
                # queue. The light-write bound, not core's hour, because this write sits in front of
                # `_notify_failure` (`tests/test_durable_observability.py`).
                schedule_to_start_timeout=light_write_queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
        except ActivityError as exc:
            # Counted, so a fleet-wide loss of the durable record shows on a dashboard rather than
            # looking
            # like a quiet deployment.
            if not workflow.unsafe.is_replaying():
                degraded(
                    logger,
                    "job_record",
                    "job record write failed for %s (%s); this run survives only in Temporal's "
                    "history",
                    record.job_id,
                    activity_failure_reason(exc),
                )
            return False
        return True


class SettleEffectInput(BaseModel):
    """The typed argument for `settle_effect_activity`."""

    effect_id: str
    state: str
    detail: str = ""
    #: The far side's handle, from the child's result envelope. Empty leaves whatever an earlier
    #: settle recorded rather than erasing it — see `_SETTLE`.
    external_ref: str = ""


@durable_activity("background")
@activity.defn
async def record_effect_activity(record: EffectRecord) -> None:
    """Write the intent to change something outside this deployment, before it is attempted."""
    await begin_effect(record)


@durable_activity("background")
@activity.defn
async def settle_effect_activity(payload: SettleEffectInput) -> None:
    """Record how an attempted effect ended."""
    await settle_effect(
        payload.effect_id,
        state=payload.state,
        detail=payload.detail,
        external_ref=payload.external_ref,
    )
