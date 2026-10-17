"""The retry policy of every activity, by the class the activity declares.

`@durable_activity(queue, ActivityClass.X)` states what an activity does to the outside world, and
a workflow dispatches it with `retry_policy=retry_for(activity)`; no call site spells a policy.
Every class is bounded, and every class but `QUEUED_TOOL` refuses bad data by type, so neither a
deterministic failure nor a down dependency is retried forever. Spacing and attempts come from
settings, read when the activity is scheduled: a retry policy is not part of a workflow's replayed
command sequence, so a changed setting affects new dispatches only.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import ActivityClass, DurableActivity, activity_class_of

# Temporal matches `non_retryable_error_types` by exact class name (not isinstance), so every
# bad-data name that can cross an activity boundary is listed, including pydantic's
# `ValidationError`. `tests/test_retry.py` asserts every `ChemclawError` subclass is either
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
    # A malformed tenant id, or a principal naming none or two (`core/tenancy.py`); retrying reads
    # the same header.
    "TenantError",
    # A sweep over the registered tenants that would pass `tenancy_sweep_max_tenants`; the registry
    # does not shrink between attempts.
    "TenantSweepError",
    # The erasure ledger unset, unreadable, or holding a line that is not a record
    # (`core/erasure_ledger.py`); none of that changes between attempts.
    "LedgerError",
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
    # A writer the skills tiers' rule does not admit, and a turn's attempt to change one; both
    # `AuthorizationError` subclasses, listed by name.
    "SkillWriteRefused",
    "SkillsReadOnlyRefusal",
    "ConnectorJobError",
    "GitWriteError",
    # A write the note store refuses for what it would do to the notes (a human's note, a stale
    # revision); the same notes are found on every attempt.
    "NoteWriteError",
    "HumanNoteProtected",
    "NoteRevisionConflict",
    "CalculationDomainError",
    # A waiter on another pod's calculation, told that the holder was refused (`science/calc/
    # flight.py`); the holder's own retry class, carried across.
    "PeerCalculationRefused",
    "ConnectorError",
    # The manifest and the server disagree about a MAJOR of the contract; the same two versions are
    # found on every attempt, so a retry is the same refusal.
    "ContractMismatch",
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
    # can ride out. `tests/test_retry.py` asserts its absence.
]


# How far *down* a first retry interval may be moved, as a fraction of it, to spread a burst of
# activities refused together. Downward only, so the jittered interval never exceeds
# `maximum_interval`, where the cap would silently remove the spread.
_JITTER = 0.25

# Temporal's own default, stated so the shape is not an accident of the SDK.
_BACKOFF = 2.0


def _first_interval(seconds: float, *, jitter: bool) -> timedelta:
    """The first retry interval, spread per run when `jitter` and inside a workflow.

    Temporal's retry schedule has no jitter, so activities refused together would retry in
    lockstep. `workflow.random()` is deterministic on replay and different between runs. Outside a
    workflow (a test, a tool) the nominal interval is returned.
    """
    if jitter and workflow.in_workflow():
        seconds *= workflow.random().uniform(1.0 - _JITTER, 1.0)
    return timedelta(seconds=seconds)


def _quick(attempts: int) -> RetryPolicy:
    """Many attempts, short and capped: a transient fault in Postgres clears in seconds."""
    return RetryPolicy(
        maximum_attempts=attempts,
        initial_interval=timedelta(seconds=settings.io_retry_initial_seconds),
        backoff_coefficient=_BACKOFF,
        maximum_interval=timedelta(seconds=settings.io_retry_max_interval_seconds),
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def _slow(attempts: int, *, jitter: bool) -> RetryPolicy:
    """Few attempts, long and capped: a dependency that is down is not hammered.

    `jitter` is for a shared dependency, where workers refused together would otherwise come back
    together; a sweep that owns its work has nothing to spread against.
    """
    return RetryPolicy(
        maximum_attempts=attempts,
        initial_interval=_first_interval(settings.slow_retry_initial_seconds, jitter=jitter),
        backoff_coefficient=_BACKOFF,
        maximum_interval=timedelta(seconds=settings.slow_retry_max_interval_seconds),
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def _calc() -> RetryPolicy:
    """Spacing sized for `CalcBusyError` (every slot taken), where a slot frees when a run ends.

    The longest interval is `calc_server_timeout_seconds`, the longest calculation waited for; the
    first is that cap divided by the doublings the attempt budget allows, so the last retry waits
    one calculation. `tests/test_retry.py` asserts wait plus backoff plus attempt fits the parent
    ceiling.
    """
    cap = settings.calc_server_timeout_seconds
    # `attempts - 2` because N attempts are N-1 retries, and the *last* of those is the one
    # that should wait a whole calculation: intervals i, 2i, 4i, 8i with 8i == cap.
    doublings = 2 ** max(settings.activity_max_attempts - 2, 0)
    return RetryPolicy(
        maximum_attempts=settings.activity_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
        initial_interval=_first_interval(cap / doublings, jitter=True),
        backoff_coefficient=_BACKOFF,
        maximum_interval=timedelta(seconds=cap),
    )


def _queued_tool() -> RetryPolicy:
    """Ask a full server again within seconds, until the call's own `schedule_to_close`.

    A queued call's worker is sized to the server's slots, so a refusal is a race with a slot about
    to free. The one non-retryable type is a fault that already spent `queued_tool_fault_attempts`
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


# The classes and what each is for (the activity-by-activity assignment is each activity's own
# decorator; `tests/test_retry.py` holds that every activity has one and every dispatch uses it):
#
#   IO           short Postgres / in-process work          io_retry_max_attempts, quick spacing
#   NOTE_WRITE   one knowledge-note write                  note_write_max_attempts, quick spacing
#   COMPUTE      long sweep or computation in the worker   activity_max_attempts, slow, no jitter
#   REMOTE       a service or system outside the worker    activity_max_attempts, slow, jittered
#   CALC         the calculation backend                   activity_max_attempts, backend spacing
#   GOVERNED     a re-run repeats audited side effects     agent_step_max_attempts, quick spacing
#   QUEUED_TOOL  a heavy tool call waiting for a slot      unbounded, bounded by its deadline
_POLICIES = {
    ActivityClass.IO: lambda: _quick(settings.io_retry_max_attempts),
    ActivityClass.NOTE_WRITE: lambda: _quick(settings.note_write_max_attempts),
    ActivityClass.COMPUTE: lambda: _slow(settings.activity_max_attempts, jitter=False),
    ActivityClass.REMOTE: lambda: _slow(settings.activity_max_attempts, jitter=True),
    ActivityClass.CALC: _calc,
    ActivityClass.GOVERNED: lambda: _quick(settings.agent_step_max_attempts),
    ActivityClass.QUEUED_TOOL: _queued_tool,
}


def child_workflow_retry() -> RetryPolicy:
    """Bounded restarts of a child workflow that is small and independent of its siblings.

    Used by the fan-out and by the hypothesis calculation child. The attempt bound is what acts: a
    child failing through its activity surfaces as a child failure whose outermost type is the
    child's, not the activity's error, so the bad-data list classifies only a failure raised by the
    child's own code.
    """
    return RetryPolicy(
        maximum_attempts=settings.activity_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def child_workflow_once() -> RetryPolicy:
    """One attempt for a child whose activities already carry the retry, so a rerun only repeats."""
    return RetryPolicy(maximum_attempts=1)


def policy_for(activity_class: ActivityClass) -> RetryPolicy:
    """The retry policy of a class, built from the current settings."""
    return _POLICIES[activity_class]()


def retry_for(activity: DurableActivity) -> RetryPolicy:
    """The retry policy for dispatching `activity`, from the class it declared.

    Passed as `retry_policy=` at every `execute_activity`, `start_activity` and local-activity
    call. Raises `LookupError` for a function that declared no class, which `tests/test_retry.py`
    makes unreachable.
    """
    return policy_for(activity_class_of(activity))
