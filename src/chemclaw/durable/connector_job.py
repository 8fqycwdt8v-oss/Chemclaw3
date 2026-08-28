"""The one durable wrapper every connector job runs inside — core keeps the cross-cutting concerns.

Why this exists: before connectors, four bespoke adapters (`agents/qm_tools.py`,
`agent/durable_tools.py`) each re-implemented the same shape — derive a deterministic id, stamp the
actor, start a named workflow, map its status, publish a note through the PR-gate, push back to the
launching session — and each imported its workflow class directly, which forced every durable
capability into core's own worker lists (`durable/background_worker.py`).

This workflow inverts that. The connector owns its workflow *code* and the worker that serves it;
core owns the obligations that must never vary per capability:

- **Idempotency** — the wrapper's id is derived from the job and its arguments by
  `chemclaw.connectors.jobs`, with `ALLOW_DUPLICATE_FAILED_ONLY`, so re-asking joins the existing
  run and
  only a failed one re-executes (D-011: a stored result is never recomputed).
- **Attribution** — the requesting actor travels in the payload (F4-T3), exactly as the removed
  `QMJobInput`
  carries `requested_by`, so an audit can always name the user behind a durable run. It is handed
  down to the child on its **memo**, not in its argument, so a bundle whose backend runs under a
  shared service identity (a calculation backend) can still name the user without the actor
  becoming a field the model could author.
- **The PR-gate** — a job that produces knowledge returns a `Note` and core publishes it through
  `chemclaw.kg.pr_gate` (via the existing `publish_memory_note_activity`). A connector never writes
  to the
  graph itself, so "the agent proposes, a human decides" cannot be bypassed by adding a connector.
- **Session push-back** — the launching chat is woken through the one existing channel (F3-T3), so
  a connector job surfaces in the UI exactly as a QM job does, with no per-connector plumbing.
- **The durable record** — what ran, on what arguments, what came out, and *why it was asked for*
  is written to `job_records` (D-157), because a workflow result is not an archive: Temporal
  expires a closed run's history and the result goes with it. Here for the same reason the other
  three are: it must hold for every capability, and "each connector remembers" is the discipline
  that fails silently.

The child is addressed by **workflow type name + task queue**, two plain strings, so this module
imports nothing from any connector. The type name comes from the manifest (`JobSpec.workflow`); the
queue is *derived* from the connector's name at dispatch (`connectors/queues.py::bundle_queue`) and
is deliberately no longer declarable — D-150 deleted the field, because a queue a bundle could name
was a queue a bundle could name wrongly, and the failure was silent. This sentence used to advertise
both as manifest strings and to offer "moving a workflow between workers is a one-line manifest
change"; the 2026-08-05 review measured `task_queue` at zero occurrences in
`connectors/manifest.py`, so the offer had been false since D-150 landed.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import degraded
    from chemclaw.durable.job_metrics import job_ended, job_running
    from chemclaw.durable.job_record import JobRecord, note_with_run_provenance, record_job
    from chemclaw.durable.memory_jobs import publish_memory_note_activity
    from chemclaw.durable.notify import notify_session_best_effort
    from chemclaw.durable.publish_results import JobPublishInput, publish_job_result
    from chemclaw.kg.note import Note
    from chemclaw.memory.jobs import SynthesisUnit

from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    publish_note_best_effort,
    publish_result_best_effort,
    queue_wait_timeout,
)
from chemclaw.durable.registry import durable_workflow

# A plain module logger rather than `workflow.logger`, used only inside an `is_replaying` guard.
# `workflow.logger` exists to suppress duplicate lines on replay, and the one place below that
# needs it is already guarded — because the *metric* beside the line must not be re-counted either,
# and no adapter can do that half. One guard covering both keeps the count and the line describing
# the same event, which is the property `metrics_bridge.degraded` exists to give.
logger = logging.getLogger(__name__)


def failure_reason(exc: BaseException) -> str:
    """The application's own account of why a job failed, for a human to read.

    Public because two callers need the identical sentence: this wrapper, pushing the failure
    back to a session that has already been told the job is running, and `connectors.jobs`,
    framing a job that failed *inside* the turn's inline wait. Two walkers would be two
    answers to "why did it fail" for one failure.

    Temporal nests structurally — `ChildWorkflowError` wraps `ActivityError` wraps whatever the
    code raised — and the outer frames say only "Child Workflow execution failed" / "Activity task
    failed". So the structural frames are skipped and the *first* application-level message is
    taken.

    Only the two *workflow-side* wrappers are skipped here. A client awaiting a handle gets one more
    on top, `temporalio.client.WorkflowFailureError`, and that one is stripped by the caller
    (`connectors.jobs`) rather than here: this module is imported inside the workflow sandbox, and
    reaching for the client package to name a type would drag the whole client into it for a string.

    **Not the innermost one**, which is the version this function shipped with and which a live run
    corrected within the hour. For the `compare_solvents` failure the chain was:

        ChildWorkflowError → ActivityError
          → "unknown ALPB solvent '2-methyltetrahydrofuran'; common valid names are water, …"
            → "String value for epsilon was not found among database of solvents"

    Walking to the bottom returned the library's internals — true, and useless to the chemist who
    typed "2-MeTHF" — while the frame directly above was the sentence the product had deliberately
    written for exactly this moment, naming the offending value and the accepted ones. Depth is not
    specificity: the deepest frame belongs to whoever is furthest from the user.
    """
    cause: BaseException = exc
    while isinstance(cause, (ChildWorkflowError, ActivityError)) and cause.__cause__ is not None:
        cause = cause.__cause__
    return str(cause) or type(cause).__name__


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
    # **Why this run was asked for**, in the requester's own terms (D-157). Required, and
    # deliberately *not* part of `payload`: the payload is hashed into the idempotency key, so a
    # rationale there would make two identical campaigns launched for differently-worded reasons
    # two separate expensive runs. It is the one fact no other store in this system held — a note
    # records what a job produced (output-neutral by design, D-005) and `audit_events` records
    # that a tool was called, but neither says what question the run was meant to answer, which is
    # exactly what is needed months later to judge whether the result still applies.
    rationale: str = Field(min_length=1)
    # The Entra actor this run is attributed to (`require_actor` at the tool boundary guarantees it
    # is present under Entra). Carried in the payload rather than read ambiently, because a workflow
    # has no request context — the same reason `QMJobInput.requested_by` exists.
    requested_by: str = Field(min_length=1)
    # The chat to wake on completion; empty off the service path (CLI, tests), where there is no
    # session to push back to.
    session_id: str = ""
    # The turn that launched this run, so its durable execution joins to the audit trail of the
    # conversation it came from (REV-11). It travelled no further than this process before: core
    # stamped every in-core tool call with a correlation id and then started a workflow that knew
    # nothing about it, so a durable job was an island in the trail. Empty off the request path,
    # where there is no turn to correlate to.
    correlation_id: str = ""
    # The plan step this run was launched for — the first `in_progress` todo at launch — and the
    # identity of the plan revision it belonged to (D-2026-08-27). Read ambiently at the launch
    # site like `session_id` above, never model-authored, and empty for every run not launched
    # from a plan step (a template step, the CLI, a turn with no plan). Additive and defaulted
    # because they cross the Temporal wire and histories are in flight.
    plan_step: str = ""
    plan_hash: str = ""
    publish_to_graph: bool = False


class ConnectorJobResult(BaseModel):
    """The result envelope every connector workflow returns — the whole cross-process contract.

    `summary` is the one line the chat shows and the model reads; `data` is the job's own structured
    result, opaque to core (a connector's domain types stay the connector's business); `note` is the
    optional knowledge contribution. Typing `note` as the existing frozen `Note` means a connector's
    proposal passes the graph's own slug and schema validators on the way in, so a malformed note is
    rejected at the boundary instead of failing later at branch creation in the PR-gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    note: Note | None = None
    # The calculation keys this run rested on, so a conclusion drawn from it can cite them
    # (D-2026-08-21). `propose_knowledge_note`'s `calc_refs` argument has told the model to "get
    # them from a job's result envelope" since D-133 and no envelope carried any: the only
    # producers in `src/` were the BO featurizer and the QM workflow's own note, neither of which
    # an agent drafting a note from a calculation it just ran can reach. Without them a stale
    # calculation cannot be traced to the conclusions drawn from it, which is the whole property
    # `calc_refs` exists for.
    #
    # Additive and defaulted, because this crosses the Temporal wire and histories are in flight —
    # the same rule `SpeciesEnergy.method` follows. Empty means "this job recorded none", never
    # "it used none".
    calc_refs: list[str] = Field(default_factory=list)
    # The name of the pydantic model `data` was dumped from — the one thing `data: dict[str, Any]`
    # destroys and nothing downstream can recover. `chemclaw.publish` dispatches on it exactly
    # (`PAYLOAD_PROJECTORS`), falling back to inferring a projector from a `calc_type` prefix; a
    # composite has no cache key and therefore no `calc_type` to infer from, so without this field
    # **every composite is silently dropped** — measured: all four shipped jobs resolved to no
    # projector, which is the case the publish seam was built for.
    #
    # Set from `type(result).__name__` at the site that still holds the typed result, never guessed
    # downstream from the connector and job names: those are a *route*, and two routes may return
    # one shape while one route's return type may change without its name changing.
    #
    # Additive and defaulted for the same reason `calc_refs` above is: it crosses the Temporal wire
    # and histories are in flight. Empty means "this job did not say", which is what every history
    # written before this field existed will decode to, and which the projector treats as "infer".
    payload_kind: str = ""


def envelope_from_result(job_id: str, raw: Any) -> ConnectorJobResult:
    """Decode what a finished durable job returned, or say why it is not a job of ours.

    Every place that collects a finished job's result goes through here — `completed_job_status`
    for the two waiters in `chemclaw.agent`, and the in-turn wait in `chemclaw.connectors.jobs`.
    They used to validate the envelope separately and diverged on the same bad input: one raised
    a written sentence, the other let pydantic's `ValidationError` escape. That is not a cosmetic
    difference, because a `ValidationError` **is** a `ValueError`, which is the family
    `_sanitize_tool_errors` deliberately passes through unchanged — so the second path relayed
    "2 validation errors for ConnectorJobResult" and pydantic's field dump to a chemist verbatim.

    Raising `ValueError` keeps that pass-through, now with a sentence written to be read.

    Args:
        job_id: The workflow id the result belongs to, for the message.
        raw: Whatever the workflow returned, undecoded.

    Raises:
        ValueError: When the result is not the connector envelope.
    """
    try:
        return ConnectorJobResult.model_validate(raw)
    except ValidationError as exc:
        # Hard, not a degraded status. Every launcher this system exposes produces the envelope,
        # so a result that is not one is a foreign workflow id. Reporting "completed" with no
        # result would announce a finished calculation while withholding the answer, which is the
        # failure mode the envelope was adopted to end (D-118).
        raise ValueError(
            f"durable job {job_id!r} completed but did not return the connector job envelope; "
            "the id does not belong to a job any launcher in this system started"
        ) from exc


def child_workflow_id(suffix: str) -> str:
    """The id for a child of the *current* execution: parent id, parent run id, then `suffix`.

    **The run id is what makes a retry possible.** Both parents that start children — this wrapper
    and `TemplateWorkflow` — are launched under a deterministic, payload-derived workflow id with
    `ALLOW_DUPLICATE_FAILED_ONLY`, precisely so a *failed* run may re-execute under the same id
    (D-011). A child named from the parent's workflow id alone is therefore named identically on
    the second execution, and `REJECT_DUPLICATE` refuses a closed id regardless of how it closed —
    so the re-execution the parent's policy exists to permit died immediately with
    `WorkflowAlreadyStartedError`, having done no work, and stayed dead until the closed child
    aged out of namespace retention. On the template path it was worse: every step that had
    *succeeded* in the first execution held its id too, so a retry could not even reach the step
    that failed.

    `REJECT_DUPLICATE` stays, and that is the point of fixing this here rather than by widening the
    policy to `ALLOW_DUPLICATE`. What the original policy wanted is a real invariant — one child
    per parent execution, so a second start of the same id is a bug and not a silent re-run — and
    it is scoped to an execution, which is exactly what the run id names. Widening the policy would
    have bought the retry by giving that invariant up.

    Replay-safe: `run_id` identifies the execution and is read from the same history a replay
    replays, so it is stable within a run and different across runs — the two properties this
    needs, and neither of them available from anything else the workflow can see.
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

    A module-level function rather than a block inside the workflow because it is pure, and
    because everything around it needs a live Temporal server to exercise — this way "the record
    carries the arguments, the *whole* result and the note it proposed" is a property the offline
    suite can hold, instead of one that is only ever checked in CI.
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
) -> JobRecord:
    """The durable record of a run that ended badly — what was asked for, and why it broke.

    **The half of `job_record_for` that did not exist.** A failing job raises before `_finish`, so
    it wrote no row at all: measured on a live broker, one `ConnectorJobWorkflow` run twice — once
    succeeding, once failing on a `ValueError` — left one `job_records` row for two jobs. Every
    question this table exists to answer months later ("what did we try", "why was it run") had no
    answer for exactly the runs somebody would go looking for, and the flagship interaction had no
    failure rate anywhere.

    A separate function rather than a `result=None` branch in `job_record_for`, because there is no
    envelope to take one from: `ConnectorJobResult.summary` is `min_length=1`, so a failure cannot
    be expressed as an empty result without loosening the contract that makes a *successful*
    envelope trustworthy. What the two share is the launch input, and that is what both copy.

    `summary` stays empty and the reason goes in `failure_reason`, deliberately: `summary` is the
    one line a listing shows for what a run produced, and a listing that cannot tell a result from
    a failure is the ambiguity the pair of columns exists to remove.
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
        state="failed",
        failure_reason=reason,
    )


# On the light queue: this wrapper does no work itself — it starts a child on the
# connector's own queue and waits — so it belongs with the many light workers, not the few
# heavy ones. The *capability* is heavy; this is not (D-006).
# The four things the wrapper still does *after* its child returns, each one activity's worth of
# wall clock: write the durable record (D-157), offer the composite to the results store, PR-gate
# the note, push back to the launching session. They are why the wrapper is not a pass-through, and
# why anyone giving it an execution timeout must leave room for them.
_FINISH_STEPS = 4


def wrapper_execution_timeout() -> timedelta:
    """A ceiling for the *wrapper*, strictly above the one it hands its own child.

    A caller that bounds `ConnectorJobWorkflow` at exactly `connector_job_timeout_seconds` — the
    number the wrapper then gives its child — leaves **zero** headroom, and since the wrapper
    starts first its ceiling expires first. A workflow execution timeout is not delivered to
    workflow code, so the `except BaseException -> _notify_failure` clause that exists precisely to
    stop a job failing in silence never runs: measured, the run ends `TIMED_OUT` with no push-back
    and no `job_records` row, which is the "a failure that says nothing is read as proceed" defect
    through the one door that clause cannot cover.

    The direct path (`connectors/jobs.py`) gives the wrapper no execution timeout at all and is
    right to — the child is already bounded. This exists for the template path, which wants a
    ceiling on the step and must not make it the child's own.
    """
    return timedelta(
        seconds=settings.connector_job_timeout_seconds
        + settings.activity_timeout_seconds * _FINISH_STEPS
    )


@durable_workflow("background")
# **`failure_exception_types` because without it this workflow cannot fail — it hangs.** The
# Temporal SDK treats a plain exception raised in workflow *code* as a suspected bug and suspends
# the run in an internal workflow-task-failure loop that ignores the retry policy and never gives
# up. This wrapper raises plain exceptions of its own: chiefly the `result_type=ConnectorJobResult`
# decode of whatever the bundle's workflow returned. Measured against a live broker: a child
# returning a non-envelope left the parent RUNNING indefinitely — history repeating
# `workflow_task_failed: "Failed decoding arguments"` every ~10 s, the worker re-polling the
# poisoned task forever, no `job_failed` push-back, and `get_durable_job_status` answering
# "running" for a job that will never finish. The parent carries no `execution_timeout` of its own,
# so nothing ends it.
#
# `TemplateWorkflow` already fixed exactly this (REV-13) and `durable/orchestrator.py` documents the
# trap (D-093); the one wrapper *every* connector job runs through had neither.
#
# The trade this makes explicit: a genuine code bug in a redeploy now fails the in-flight jobs
# instead of parking them until someone ships a fix. That is the right way round here — a job that
# hangs forever while telling a chemist it is running is the worse failure, and the same judgement
# was already made one module over.
@workflow.defn(failure_exception_types=[Exception])
class ConnectorJobWorkflow:
    """Run one connector-owned workflow as a child, then publish and notify on its behalf."""

    @workflow.run
    async def run(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Execute the connector's workflow, PR-gate any note it produced, and wake its session.

        The child runs on the connector's own task queue, so its dependencies and its failure domain
        stay outside this worker. A child failure propagates: the job genuinely failed, and the tool
        that launched it reports `failed` through `get_durable_job_status` — deliberately unlike the
        note publish and the push-back below, which are best-effort because the scientific result is
        already durable by the time they run.

        **A failure is pushed back to the session before it propagates.** Propagating is correct;
        propagating *silently* was not. A job that outlives its turn has already told the chemist
        "this is running", and the only completion path back to them was `job_completed` — so a job
        that failed afterwards left that promise standing forever, with the failure visible only
        to someone who thought to poll `get_durable_job_status` with an id they would have had to
        keep. Measured on 2026-08-04: `compare_solvents` was launched for a three-solvent screen,
        the turn reported it running, and the run failed ~30 s later on an unknown ALPB solvent
        name. Nothing reached the asker. Same lesson as the unreachable broker that reached the
        model as "Error: Function failed." — an outcome that says nothing is not neutral, it is an
        invitation to assume the good one.
        """
        # `workflow.now()` and not `time.monotonic()`: a workflow's clock must come from the one
        # Temporal records in history, or a replay would measure the replay rather than the run.
        started_at = workflow.now()
        # This process is now carrying the run. Idempotent under replay by construction — see
        # `durable/job_metrics.py` — so it needs no `is_replaying` guard, unlike every *counter*
        # a workflow body touches.
        job_running(workflow.info().workflow_id)
        try:
            result = await self._run_child(job)
            return await self._finish(job, result, started_at)
        except BaseException as exc:
            # **Every way this run can end badly, not only a failing child.** The clause used to be
            # `except (ChildWorkflowError, ActivityError)` around the child call alone, which is a
            # correct account of *the child* failing and covers nothing else: the envelope decode,
            # `job_record_for`, `note_with_run_provenance` and the two best-effort steps all raise
            # outside it. So the moment `failure_exception_types` above turns the measured hang into
            # a real failure, that failure would arrive at the chemist as silence — the same
            # `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` defect through a door
            # the narrow clause does not cover. A job that outlives its turn has already been
            # announced as running, and the only path back is this notification.
            #
            # `BaseException` rather than `Exception` so a cancellation is announced too (measured:
            # the parent reaches CANCELED and the session gets `job_failed reason="Cancelled"`),
            # and `_notify_failure` never raises, so a broken push-back cannot replace the real
            # reason with its own.
            # **Written before the push-back, and for the same reason `_finish` writes its record
            # before publishing the note**: this row is the durable copy, and the notification is a
            # message to a session that may no longer be listening. A failed run used to leave
            # neither — it existed only in Temporal's expiring history, so a job whose failure
            # push-back was dropped left literally nothing behind.
            await self._record_run(
                failed_job_record(
                    workflow.info().workflow_id,
                    job,
                    failure_reason(exc),
                    (workflow.now() - started_at).total_seconds(),
                )
            )
            await self._notify_failure(job, exc)
            raise
        finally:
            job_ended(workflow.info().workflow_id)

    async def _run_child(self, job: ConnectorJobInput) -> ConnectorJobResult:
        """Start the bundle's own workflow on its queue and wait for its result."""
        result: ConnectorJobResult = await workflow.execute_child_workflow(
            job.workflow,
            job.payload,
            id=child_workflow_id("run"),
            task_queue=job.task_queue,
            result_type=ConnectorJobResult,
            # The actor, carried as per-execution metadata rather than in the argument. A bundle
            # whose backend runs under a *shared* service identity — a calculation backend is the
            # one we
            # have — must still be able to name the user behind a run, and `payload` is exactly the
            # model-authored arguments, so putting the actor there would make it a field the LLM
            # could fill in. A memo is beside the argument, readable with `workflow.memo_value`,
            # and set once here for every connector job rather than per bundle (D-118).
            # `correlation_id` rides beside the actor for the same reason the actor does: it is
            # metadata about the run, not a model-authored argument, and `payload` is exactly the
            # arguments the LLM filled in. A memo keeps both readable (`workflow.memo_value`)
            # without letting either become something the model can write.
            memo={"requested_by": job.requested_by, "correlation_id": job.correlation_id},
            # A child is started once per parent *execution* — which is what `child_workflow_id`
            # names, and why rejecting duplicates is still the honest policy here: within one
            # execution a duplicate id is a bug, and across executions the id differs so a failed
            # parent can genuinely re-run. See `child_workflow_id` for the retry this used to block.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            # **One attempt, because a workflow-level retry here can only duplicate compute.**
            # `BAD_DATA_RETRY` cannot classify anything at this boundary: Temporal matches
            # `non_retryable_error_types` against the *outermost* failure, and a child that failed
            # through its own activity surfaces as `ActivityFailure` — a name deliberately absent
            # from `_BAD_DATA_TYPES`, since that list names the errors themselves. Measured: the
            # identical `ValueError` costs 1 attempt at an activity boundary and **5 child
            # executions** here, 15.4 s of backoff, six executions under one parent id. On a
            # that is five DFT submissions for one unparameterised basis set, and the D-011 cache
            # cannot help because a failed run stores nothing.
            #
            # Nothing is lost by dropping it: the child's own activities already carry
            # `BAD_DATA_RETRY`, so genuine transients are retried where they can be classified, and
            # a worker that dies mid-child is re-delivered by Temporal without any workflow retry.
            retry_policy=RetryPolicy(maximum_attempts=1),
            execution_timeout=timedelta(seconds=settings.connector_job_timeout_seconds),
        )
        return result

    async def _publish_result(self, job: ConnectorJobInput, result: ConnectorJobResult) -> None:
        """Offer this run's own result to the external results store, if one is configured.

        The envelope's `data` is the composite the job produced - a reaction energy, a solvent
        screen, an ensemble - which is precisely the shape that has no `calculation_results` row
        and therefore reaches a results store through no other path.

        `calc_ref` is the workflow id rather than a cache key, because a composite has no cache
        key: its identity is the run. That is also what makes it idempotent, since the workflow id
        is itself derived deterministically from the job and its arguments.

        Runs through an activity rather than inline: a workflow may not touch a database, and
        `publish_result_activity` carries the same bounded retry every other best-effort step here
        uses.
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
                )
            ],
            label=f"{job.connector}:{job.job}",
        )

    async def _notify_failure(self, job: ConnectorJobInput, exc: BaseException) -> None:
        """Tell the session its job failed, before the failure propagates and closes this run.

        Best-effort and never raising, for the same reason the completion push-back is: the run is
        already failing, and a push-back that failed on top would replace one lost message with two.
        The reason is carried as text because that is what the asker needs — the same discipline
        `SubsystemUnavailableError` applies to an outage, one layer out.
        """
        if not job.session_id:
            return
        await notify_session_best_effort(
            job.session_id,
            "job_failed",
            {
                "job_id": workflow.info().workflow_id,
                "connector": job.connector,
                "job": job.job,
                "reason": failure_reason(exc),
            },
        )

    async def _finish(
        self, job: ConnectorJobInput, result: ConnectorJobResult, started_at: datetime
    ) -> ConnectorJobResult:
        """Record the run, offer its note to the PR-gate, and push the completion back."""
        record = job_record_for(
            workflow.info().workflow_id,
            job,
            result,
            runtime_seconds=(workflow.now() - started_at).total_seconds(),
        )
        # Written *before* the note publish, because this is the durable copy: the graph write is a
        # proposal a human may never merge, while this row is what makes the result survive
        # Temporal's own history retention. Best-effort for the same reason the publish is — the
        # science is finished, so a database that is down must not fail a completed job and send an
        # expensive campaign round the retry loop — but logged at error level, because unlike a
        # failed note this loses data nothing else holds.
        await self._record_run(record)
        # The external results store, if a deployment has one. Beside the durable record and before
        # the note, because it is the same kind of obligation the other two are: cross-cutting, and
        # "each connector remembers" is the discipline that fails silently. Best-effort for the
        # same reason as its neighbours — the science is already durable by the time this runs.
        #
        # This is the hook that reaches the *composites*. The primitives a job consumed were each
        # published by `cached_compute` as they were computed; what only exists here is the
        # composite the job assembled from them, which has no cache row of its own by design.
        await self._publish_result(job, result)
        if job.publish_to_graph and result.note is not None:
            # The same PR-gate activity the memory-synthesis jobs use — one write path into the
            # graph, on the light background queue, bounded retries, never failing the job. The
            # note is stamped with the run and its reason on the way through, here rather than in
            # each connector, so no bundle can forget and every merged note answers "why was this
            # done" as well as "what came out".
            # `job.requested_by` travels with the note so the proposal is recorded against the
            # chemist who launched the job. Without it `ambient_provenance()` yields `actor=""`,
            # the row is invisible in that chemist's own review queue, and the PR opened on their
            # behalf is one they cannot find — while the input carrying their identity sits one
            # frame above, required and unused.
            await publish_note_best_effort(
                publish_memory_note_activity,
                [
                    # A connector job never retires anything — retirement is the synthesis
                    # miners' judgment — so its unit carries the note alone.
                    SynthesisUnit(note=note_with_run_provenance(result.note, record)),
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
        return result

    async def _record_run(self, record: JobRecord) -> None:
        """Persist the run's durable record, logging rather than failing the job if it cannot be.

        A method rather than an inline block so the "never fail a finished job" decision has one
        place to be read and one place to change — the same shape, and the same reasoning, as
        `publish_note_best_effort`.
        """
        try:
            await workflow.execute_activity(
                record_job,
                record,
                # Named explicitly although this workflow already runs there: the activity is
                # registered on the background queue alone, so were the wrapper ever moved, the
                # default would route the write to a queue where nothing serves it — a silent
                # loss, discovered when an id expires months later.
                task_queue=settings.background_task_queue,
                start_to_close_timeout=timedelta(seconds=settings.job_record_timeout_seconds),
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
        except ActivityError:
            # **Counted, not just logged.** The line below has always said this run "survives only
            # in Temporal's history", i.e. that the swallow loses data nothing else holds — and it
            # was one of the ~30 modules whose deliberate swallows were invisible to anything but a
            # log search nobody runs (`metrics_bridge.degraded`). A fleet-wide loss of the durable
            # record — a dead background queue, a full table — produced exactly what a quiet
            # deployment produces.
            if not workflow.unsafe.is_replaying():
                degraded(
                    logger,
                    "job_record",
                    "job record write failed for %s; this run survives only in Temporal's history",
                    record.job_id,
                )
