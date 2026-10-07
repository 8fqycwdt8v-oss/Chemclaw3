"""Shared workflow-side retry and timeout discipline for durable activities.

`BAD_DATA_RETRY` fails fast on bad data (Temporal matches non-retryable types by exact class
name, so every bad-data class is listed) and bounds transient retries. The note and result
publish helpers run on the light background queue and, for best-effort publishes, never fail a
completed scientific result. The queue-wait bounds say how long an activity may sit unclaimed,
in three sizes: `queue_wait_timeout` (core's hour), `connector_queue_wait_timeout` (a bundle's,
where a wait is backpressure) and `light_write_queue_wait_timeout` (end-of-job writes).
`calculation_retry` adds a backoff sized to a full calculation backend.
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric

# Temporal matches `non_retryable_error_types` by exact class name (not isinstance), so every
# bad-data name that can cross an activity boundary is listed, including pydantic's
# `ValidationError`. `tests/test_publish.py` asserts every `ChemclawError` subclass is either
# listed here or declared retryable below.
_DECLARED_RETRYABLE = frozenset(
    {
        # Not in the bad-data list: `kg/git_writer.py`'s `GitRemoteError` (dead remote, timeout,
        # contended lock) is the retryable half of git write failures.
        "GitRemoteError",
    }
)

_BAD_DATA_TYPES = [
    "ValueError",
    "ValidationError",
    "ChemclawError",
    "InvalidSmilesError",
    "FingerprintError",
    # The argument was not fingerprintable; listed beside its parent because names, not the
    # hierarchy, are matched.
    "FingerprintInputError",
    # A structure asked of a citation-only reaction (`ingest/eln/ord.py`): the source named a
    # species without its structure, and a retry finds the identical record.
    "StructureNotGiven",
    # A fork of a session with no saved state; retrying finds the same absence.
    "SessionForkError",
    # The proposal store's insert-or-conflict found no row; deterministic against the same rows.
    "ProposalStoreError",
    # A document the personal skills tier will not keep; the admission rules are deterministic.
    "SkillRefused",
    # A `reaction_records.conditions` payload that is not a JSON object; retrying re-reads the same
    # row.
    "UnreadableConditions",
    "ElnMappingError",
    "ElnFormatError",
    "OrdFormatError",
    "IngestError",
    # An activity result over `activity_result_max_bytes` (`durable/interceptor.py`); the result is
    # deterministic in the arguments, so a retry is the same size.
    "ActivityResultTooLarge",
    "MetricError",
    "PlaybookError",
    # A campaign's recorded points and its decision space disagreeing, or a design space that cannot
    # be expressed as factors; permanent properties of the two documents.
    "BoTranslationError",
    "NoteError",
    # A delivery channel with no folder or a `config:` the driver refuses; an unreachable
    # destination raises from the driver instead and stays retryable.
    "DeliveryChannelError",
    "EvalCaseError",
    # A run scored against a baseline recorded on a different case-set; stays impossible until a
    # person refreshes one of them.
    "CaseSetMismatchError",
    # A tool answered with `isError=True`: the server gave its verdict. `CalcServerError` (nobody
    # answered) is the retryable neighbour.
    "ToolReturnedFailure",
    # A tool call the model mis-serialised; the arguments are fixed, so a retry re-reads the same
    # document. In practice `surface_domain_errors` converts it to a `ToolMessage` before it can
    # cross an activity boundary.
    "UnparsedArguments",
    # A turn tried to change the skills tree; an `AuthorizationError` subclass, listed by name.
    "SkillsReadOnlyRefusal",
    "ConnectorJobError",
    "GitWriteError",
    "CalculationDomainError",
    "ConnectorError",
    "DataSourceError",
    # Two sources transcribed the same entry id; the ambiguity is a fact about the corpus.
    "AmbiguousReactionRecord",
    # A label written for a reaction with no stored record phase; retrying finds the same absence.
    "LabelIndexError",
    # The labelling server refused the input; `LabelServerError` (nobody answered) is the retryable
    # sibling.
    "LabelToolError",
    # An unresolvable sink manifest, or a record a destination answered and refused.
    # `SinkUnavailableError` is a `ConnectionError` and stays retryable.
    "ResultSinkError",
    "SinkRejectedError",
    "SinkConnectionError",
    "ProjectionError",
    "UnknownPropertyError",
    # An offboarding erasure the database refused, or one on a blank actor; both facts about the
    # request or the deployment.
    "ErasureError",
    # A vector store that cannot be built as configured (missing package, unknown provider).
    # `VectorStoreError` (unreachable) stays retryable.
    "VectorStoreConfigError",
    # The prescriptive-design tier: a plate that cannot hold the arms, an unknown design id, a
    # revision derived from a stale head. `RevisionConflict` and `StatusConflict` look transient but
    # are not: retrying would resolve the race by discarding the revision or decision it did not
    # see.
    "LayoutError",
    "RevisionConflict",
    "StatusConflict",
    "UnstorableDocument",
    "UnknownDesign",
    # An outcome naming an arm the stored revision does not have.
    "UnknownArm",
    # The latest values for one outcome in more than one unit. The stored rows decide it, so a
    # retry reads the same rows and refuses identically.
    "MixedUnits",
    # An artefact write the store refuses; decided by the stored rows and the request.
    "InvalidExhibit",
    "StaleRevision",
    "UnknownExhibit",
    "ExhibitLimit",
    "TemplateError",
    # A composed workflow that names a write, a job, or a step that does not resolve. Bad data in
    # exactly this list's sense: the document is what is wrong, so every attempt fails identically.
    "ComposedWorkflowError",
    "UnresolvedReference",
    "ProfileError",
    # A surrogate fit or acquisition step failed on the given observations; deterministic in the
    # data.
    "SurrogateFitError",
    # A declaratively-bound warehouse source failing deterministically: a malformed binding, a value
    # outside the binding's vocabulary, a missing relation or column, or a `regex` transform that
    # exhausted its time budget. An unreachable warehouse raises `ConnectionError` and stays
    # retryable.
    "BindingError",
    "PathSyntaxError",
    "PatternBudgetError",
    "TransformError",
    "WarehouseQueryError",
    # A vendored dataset that is absent, malformed or fails its checksum; the fix is a rebuild.
    "VendoredDatasetError",
    # A mounted document share that cannot be read as declared (malformed binding, not a directory).
    "DocumentShareError",
    # The calculation server was reached and refused. `CalcServerError` (unreachable) is a
    # `SubsystemUnavailableError` and stays retryable.
    "CalcToolError",
    # The server's inline time budget stopped the calculation; a retry hits the same clock.
    "CalcTimeBudgetError",
    # A turn repeated an identical tool call too often; identical on retry too.
    "RepeatedCallRefusal",
    # `AuthorizationError` and its subclasses are not `ChemclawError`s (a refusal is policy, not bad
    # data) but never change on retry, so they are listed by name; `authorize_job_step` raises one
    # across a real activity boundary.
    "AuthorizationError",
    "DryRunRefusal",
    "PlanNotApprovedError",
    # An agent step reached for a write its template did not declare; the declaration is pinned in
    # the run's input.
    "UndeclaredWriteRefusal",
    # Deliberately absent: `SubsystemUnavailableError` means an unreachable subsystem, which a retry
    # can ride out. `tests/test_publish.py` asserts its absence.
]

# Bad data is non-retryable by type; `maximum_attempts` bounds transient retries so an
# unclassified deterministic failure gives up instead of pinning a worker forever.
BAD_DATA_RETRY = RetryPolicy(
    maximum_attempts=settings.activity_max_attempts,
    non_retryable_error_types=list(_BAD_DATA_TYPES),
)


def note_publish_retry() -> RetryPolicy:
    """Bounded retries for a note write (config `note_write_max_attempts`).

    Shares the bad-data list, so a bad note or a structural `GitWriteError` fails fast;
    `GitRemoteError` (dead remote, timeout, contended lock) is the retryable subclass.
    """
    return RetryPolicy(
        maximum_attempts=settings.note_write_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def agent_step_retry() -> RetryPolicy:
    """A narrow outer bound for the one activity whose retry is not free.

    Config `agent_step_max_attempts`. A retry of a template's agent step replays the whole turn,
    re-running every tool and its side effects (duplicate notes, duplicate audit rows), so attempts
    are fewer. The bad-data list is unchanged: a provider 503 stays retryable, and the SDK already
    retries a blip internally. A long provider outage fails the step sooner; a person re-runs it.
    """
    return RetryPolicy(
        maximum_attempts=settings.agent_step_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def queue_wait_timeout() -> timedelta:
    """How long a core activity may sit unclaimed on its queue, as `schedule_to_start_timeout`.

    `start_to_close_timeout` starts only once a worker picks the task up, so on an unserved queue
    it bounds nothing, and a Schedule under SKIP then skips every later fire. Schedule-to-start is
    not retried and leaves the retry budget intact, unlike schedule-to-close, which caps all
    attempts together. A function so the setting is read at run time, not import time.

    Returns:
        The `schedule_to_start_timeout` every core activity call passes.
    """
    return timedelta(seconds=settings.activity_queue_wait_seconds)


def light_write_queue_wait_timeout() -> timedelta:
    """How long a *small* write may wait on the shared background queue, before it is a fault.

    For the end-of-job writes (session push-back, job record, outbound copy), which callers swallow;
    an hour of patience there is an hour a finished or failed job reports nothing.
    `tests/test_activity_queue_bound.py` holds every dispatch site to its bound.

    Equal to `template_step_timeout_seconds`, the longest single activity this queue runs and so
    the worst a slot holder can put in front of a small write. Derived rather than configured so the
    relationship holds. The caller's `start_to_close_timeout` bounds the work separately.

    Returns:
        The `schedule_to_start_timeout` every end-of-job write passes.
    """
    return timedelta(seconds=settings.template_step_timeout_seconds)


def fan_out_queue_wait_timeout() -> timedelta:
    """How long a **fan-out child's** activity may sit unclaimed before the child gives up.

    The wait precedes the work, so the child's ceiling `C` must contain wait plus work. This takes
    the ceiling's headroom, `C - w - overhead`, where `w` is `longest_fan_out_activity`, so the
    composite fits by construction and an unserved queue surfaces as a named activity timeout rather
    than a child execution timeout no workflow code sees.

    Returns:
        The `schedule_to_start_timeout` a fan-out child's activity passes. Strictly positive by
        construction: `_the_fan_out_ceiling_covers_the_section_it_bounds` refuses a ceiling that
        does not exceed the longest fan-out activity plus one activity's overhead.
    """
    longest, _ = settings.longest_fan_out_activity
    return timedelta(
        seconds=settings.fan_out_child_timeout_seconds - longest - settings.activity_timeout_seconds
    )


def connector_queue_wait_timeout() -> timedelta:
    """How long a **connector bundle's** activity may sit unclaimed on its own queue.

    On a bundle queue a long wait is ordinary backpressure, but an unbounded one makes "no worker is
    serving `connector-calc`" indistinguishable from "every worker is busy" until the child's
    execution timeout, which is delivered to nobody.

    The bound is the ceiling's headroom, `C - w - overhead`, with `w` = `longest_bundle_activity`,
    so wait plus one attempt always fits the parent's execution budget; a fraction of `C` cannot
    guarantee that. A ScheduleToStart expiry is not retried: an unserved queue stays unserved.

    This holds for one activity. A child dispatching a sequence (`BoCampaignWorkflow`) must use
    `remaining_queue_wait_timeout` instead.

    Returns:
        The `schedule_to_start_timeout` a single-activity connector-bundle child passes. Strictly
        positive by construction: `Settings` refuses a ceiling that does not exceed the longest
        activity plus one activity's overhead.
    """
    longest, _ = settings.longest_bundle_activity
    return timedelta(seconds=_queue_wait_seconds(settings.connector_job_timeout_seconds, longest))


def _queue_wait_seconds(budget: float, activity_seconds: float) -> float:
    """What is left of `budget` for a queue wait once one attempt and its overhead are paid for.

    The one subtraction behind the bounds above and below.

    Args:
        budget: The execution budget this wait has to fit inside, in seconds.
        activity_seconds: The start-to-close budget of the attempt that follows the wait.

    Returns:
        The wait in seconds. May be zero or negative: for the deployment-wide ceiling `Settings`
        has already refused that case; for a run partway through its budget it means nothing is
        left to fund another activity.
    """
    return budget - activity_seconds - settings.activity_timeout_seconds


def remaining_queue_wait_timeout(remaining: timedelta, activity_seconds: float) -> timedelta | None:
    """The queue wait a bundle child may still afford, given what is left of its execution budget.

    For a child that dispatches a sequence: the execution timeout spans the whole continue-as-new
    chain, so the budget is spent down rather than re-granted, keeping the sum of waits and attempts
    within the ceiling for any number of steps. `None` means the remaining budget cannot fund
    another attempt, and the caller stops with what it has (raising in workflow code would retry
    forever). `activity_seconds` is the caller's own budget; callers also apply the queue-wide
    bound and take the minimum.

    Args:
        remaining: What is left of this run's execution budget.
        activity_seconds: The start-to-close budget of the activity about to be dispatched.

    Returns:
        The `schedule_to_start_timeout` for the next dispatch, or None when the budget can no
        longer fund one.
    """
    seconds = _queue_wait_seconds(remaining.total_seconds(), activity_seconds)
    return timedelta(seconds=seconds) if seconds > 0 else None


# How far *down* the first capacity retry may be moved, as a fraction of it, to spread a burst of
# jobs refused together. Downward only; `calculation_retry` says why.
_CAPACITY_RETRY_JITTER = 0.25


def calculation_retry() -> RetryPolicy:
    """The retry discipline for an activity that calls the shared calculation backend.

    `BAD_DATA_RETRY`'s types and attempt count, with spacing sized for `CalcBusyError` (every slot
    taken), where a slot frees only when a calculation finishes. The longest interval is
    `calc_server_timeout_seconds`, the longest calculation waited for; the first is that cap divided
    by the doublings the attempt budget allows. `tests/test_publish.py` asserts wait plus backoff
    plus attempt fits the parent ceiling.

    Temporal's retry schedule has no jitter, so the first interval is jittered with
    `workflow.random()`: deterministic on replay, different between runs, so jobs refused together
    do not retry in lockstep. Outside a workflow the nominal schedule is returned.

    Returns:
        The retry policy every activity that dispatches to the calculation backend passes.
    """
    cap = settings.calc_server_timeout_seconds
    # `attempts - 2` because N attempts are N-1 retries, and the *last* of those is the one
    # that should wait a whole calculation: intervals i, 2i, 4i, 8i with 8i == cap.
    doublings = 2 ** max(settings.activity_max_attempts - 2, 0)
    first = cap / doublings
    if workflow.in_workflow():
        # Downward only: upward jitter would push the last interval past `maximum_interval`, where
        # the cap silently removes the spread.
        first *= workflow.random().uniform(1.0 - _CAPACITY_RETRY_JITTER, 1.0)
    return RetryPolicy(
        maximum_attempts=settings.activity_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
        initial_interval=timedelta(seconds=first),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=cap),
    )


def queued_tool_retry() -> RetryPolicy:
    """The retry discipline for a queued tool call: ask a full server again within seconds.

    A queued call's worker is sized to the server's slots, so a refusal is a race with a slot about
    to free. Unlimited attempts, bounded by the call's own `schedule_to_close`; the one
    non-retryable type is a fault that already spent `queued_tool_fault_attempts`
    (`connectors/queued_call.py`).
    """
    cap = settings.queued_tool_retry_max_seconds
    return RetryPolicy(
        # Never above the cap: the server refuses a policy whose first interval exceeds its maximum.
        initial_interval=timedelta(seconds=min(1.0, cap)),
        backoff_coefficient=1.5,
        maximum_interval=timedelta(seconds=cap),
        maximum_attempts=0,
        non_retryable_error_types=["QueuedToolFault"],
    )


def activity_failure_reason(exc: ActivityError) -> str:
    """A short reason for a *swallowed* activity failure, so one log line separates two states.

    A `SCHEDULE_TO_START` expiry means nobody polls the queue (fixed by a worker, not a retry);
    anything else is the write itself, reported by type.

    Args:
        exc: The swallowed activity error, whose `cause` carries what Temporal decided.

    Returns:
        A sentence fragment for the caller's own log line. Never raises.
    """
    cause = exc.cause
    if isinstance(cause, TemporalTimeoutError):
        if cause.type is TimeoutType.SCHEDULE_TO_START:
            return (
                "no worker claimed the task within the configured queue wait "
                "(schedule-to-start): nothing is serving that queue, or it is backed up past "
                "the bound"
            )
        # `type` is optional on the SDK's own model, so the unnamed case is spelled rather than
        # assumed away: a timeout that does not say which one it was is still a timeout.
        which = cause.type.name.lower() if cause.type is not None else "kind unreported"
        return f"the activity timed out ({which})"
    return type(cause).__name__ if cause is not None else "unknown"


async def publish_note(activity: Any, args: list[Any]) -> str:
    """Run a note-publish activity with the shared queue/timeout/retry discipline."""
    result: str = await workflow.execute_activity(
        activity,
        args=args,
        task_queue=settings.background_task_queue,
        start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
        schedule_to_start_timeout=queue_wait_timeout(),
        retry_policy=note_publish_retry(),
    )
    return result


async def publish_note_best_effort(activity: Any, args: list[Any], label: str) -> None:
    """Publish a note but never fail the caller: log-and-swallow a failed write.

    For workflows whose real result is the calculation: a broken git remote must not fail the job.
    A failure is counted, so a dead remote is distinguishable from an idle deployment; guarded on
    `is_replaying` so a replay does not re-count.
    """
    try:
        await publish_note(activity, args)
    except ActivityError:
        workflow.logger.warning("knowledge-note publish failed for %s", label)
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_notes_publish_failures_total"))


async def publish_result_best_effort(activity: Any, args: list[Any], label: str) -> None:
    """Queue a finished run's result for the external results store, never failing the caller.

    The result is already durable in `job_records`, so an unavailable outbox must not fail a
    completed job. Separate from the note publish: different timeout, counter and meaning. The
    counter is guarded on `is_replaying`.
    """
    try:
        await workflow.execute_activity(
            activity,
            args=args,
            task_queue=settings.background_task_queue,
            start_to_close_timeout=timedelta(seconds=settings.result_publish_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
    except ActivityError:
        workflow.logger.warning("result publication failed to queue for %s", label)
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
