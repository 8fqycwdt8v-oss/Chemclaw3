"""Shared workflow-side pieces of the PR-gated note publish (gate G4/DRY).

Why this exists: three workflows (QM job, BO campaign, development report) end by
writing an agent note through the PR-gate. The retry discipline is identical for
all of them — run on the light background queue, bound the attempts so a broken
git remote gives up instead of retrying forever, and (for best-effort publishes)
never let a failed note write fail the completed scientific result. Before this
module the block was copy-pasted per workflow and the copies drifted (the report
publish shipped with no retry bound at all).

`BAD_DATA_RETRY` is the same idea for ordinary activities: a `ValueError` means
bad/corrupt data that will never succeed on retry, so fail fast (`ChemclawError`
subclasses inherit from `ValueError` but Temporal matches non-retryable types by
exact class name, so the concrete names are listed too). The queue bounds are the
other shared discipline here: one place that says how long an activity may wait for a worker to pick
it up, in three sizes because a wait means three different things. `queue_wait_timeout` is core's
hour; `connector_queue_wait_timeout` is a bundle's, where a long wait is ordinary backpressure; and
`light_write_queue_wait_timeout` is the tighter one the two end-of-job writes take, because patience
there is time a finished job has told nobody about. `calculation_retry` is the last piece —
`BAD_DATA_RETRY` with a backoff sized to the thing that is now retryable, a shared calculation
backend that is full.
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

# Temporal matches `non_retryable_error_types` by exact class name (not isinstance),
# so every bad-data name that can cross an activity boundary is listed explicitly.
# `ValidationError` (pydantic) subclasses `ValueError` but has its own class name, so
# a model-build failure on corrupt data would otherwise be treated as retryable.
#
# The completeness walk in `tests/test_publish.py` asserts every `ChemclawError` subclass is
# either listed here or *declared* retryable below — a subclass in neither set is the drift the
# walk exists to catch, while a silent exemption would be the walk defeated.
_DECLARED_RETRYABLE = frozenset(
    {
        # `kg.git_submitter.GitRemoteError`: a dead remote, a timed-out git command, a contended
        # submit lock. The transient half `GitSubmitError` used to cover with one name — which
        # made `note_write_max_attempts` dead for exactly the failures it was configured for.
        "GitRemoteError",
    }
)

_BAD_DATA_TYPES = [
    "ValueError",
    "ValidationError",
    "ChemclawError",
    "InvalidSmilesError",
    "FingerprintError",
    # The *argument* was not fingerprintable — a prose sentence where a reaction SMILES was
    # expected, an OCR artefact in an impurity list (`science.fingerprints.store`). Listed beside
    # its parent because Temporal matches by class *name*, so a subclass inherits nothing here, and
    # `tests/test_publish.py` walks the hierarchy precisely so this cannot be forgotten. Retrying a
    # string the parser has already refused finds the identical refusal.
    "FingerprintInputError",
    # A fork of a session with no saved state (`agent/session_fork.py`). Bad data rather than
    # transient: the parent has taken no turn, so there is no thread to copy, and retrying finds
    # exactly the same absence — nothing about waiting makes a checkpoint appear.
    "SessionForkError",
    # A sign-off written against a status the design has since left (`protocols/store.py`). The one
    # conflict in this list that a retry makes *worse* rather than merely useless: the design has
    # already moved, so the answer will not change until a person re-reads it and decides again,
    # which is the whole content of the refusal.
    "StatusConflict",
    "ElnMappingError",
    "ElnFormatError",
    "OrdFormatError",
    "IngestError",
    "MetricError",
    "PlaybookError",
    "NoteError",
    # A channel named in `CHEMCLAW_DELIVERY_CHANNELS` with no folder, or a `config:`
    # block the driver's signature refuses (`chemclaw.deliver.registry`). Both are a
    # deployment's declaration disagreeing with what is on disk, and a retry finds the
    # same disagreement — the destination being *unreachable* is a different failure
    # and raises from the driver, not from here.
    "DeliveryChannelError",
    "EvalCaseError",
    # A run scored against a baseline recorded on a *different* case-set
    # (`chemclaw.evals.baseline`). Bad data by the same test as every entry here: the committed
    # baseline file and the version the run declared are both facts, and the identical comparison
    # stays impossible until a person refreshes one of them.
    "CaseSetMismatchError",
    # A tool that answered a template's `tool` step with `isError=True` rather than a result
    # (`chemclaw.agent.tool_invocation`). Non-retryable because the server *answered*: it has made
    # its verdict, and the identical call gets the identical refusal. The retryable neighbour is
    # `CalcServerError`, which means nobody answered at all.
    "ToolReturnedFailure",
    # A tool call the model mis-serialised, refused by `agent/model_calls.refuse_unparsed_arguments`
    # before the body runs. It is here because `tests/test_publish.py` walks the hierarchy and every
    # `ChemclawError` must be classified, and it is *non*-retryable for the ordinary reason: the
    # arguments are a fact about the emission, so an identical retry re-reads the identical
    # unparseable document. It is also unreachable across an activity boundary, though **not** for
    # the reason this comment first gave ("no activity invokes that chain" —
    # `durable/template_activities.run_agent_step` is an `@activity.defn` that builds the agent and
    # runs a whole turn through exactly that chain). The real reason is one middleware out:
    # `surface_domain_errors` is outermost of `tool_call_middleware` and converts every
    # `ChemclawError` into a `ToolMessage`, so this never leaves the graph as a raised exception
    # whatever ran it. Either way the row is the classification rather than a live policy.
    "UnparsedArguments",
    # A turn tried to change the skills tree (`chemclaw.agent.skill_backend`). An
    # `AuthorizationError` subclass, and registered for the same reason every other one is: Temporal
    # matches by class *name*, and `tests/test_publish.py` walks that hierarchy so a subclass cannot
    # go unregistered unnoticed.
    "SkillsReadOnlyRefusal",
    "ConnectorJobError",
    "GitSubmitError",
    "CalculationDomainError",
    "ConnectorError",
    "DataSourceError",
    # Two ingest sources have transcribed the same entry id, so a citation naming it has two
    # answers (`chemclaw.ingest.eln.records`). Non-retryable because the ambiguity is a fact about
    # the corpus rather than about this attempt: the identical read finds the identical two rows
    # until a person decides which source the citation meant.
    "AmbiguousReactionRecord",
    # A derived label written for a reaction whose record phase was never stored
    # (`chemclaw.science.labels.store`). Non-retryable because the missing row is a fact about the
    # corpus, not about this attempt: the drain must record the reaction before it can label it,
    # and retrying the same write finds the same absence.
    "LabelIndexError",
    # The labelling server reached and refused: a reaction SMILES RDKit cannot parse, a species
    # list that does not match the reaction (`chemclaw.ingest.labels.labeller`). Its retryable
    # sibling is `LabelServerError`, which means nobody answered at all.
    "LabelToolError",
    # The result-publication seam (D-2026-08-25). Both are bad data by the same test as every entry
    # here: a sink whose manifest cannot be resolved, or a record a destination has *answered*
    # about and refused, fails identically on every retry. Its retryable neighbour is
    # `SinkUnavailableError`, which is a `ConnectionError` and deliberately absent from this list.
    "ResultSinkError",
    "SinkRejectedError",
    "SinkConnectionError",
    "ProjectionError",
    "UnknownPropertyError",
    # An offboarding erasure the database refused, or one asked for on a blank actor
    # (`chemclaw.agent.leaver`). Non-retryable for the same reason every entry here is: a missing
    # `DELETE ON session_owners` grant and an empty actor id are both facts about the request or the
    # deployment, and the identical call fails identically until a person changes something. Listed
    # even though no workflow runs an erasure today — the list is keyed by *class name*, so an entry
    # that is never matched costs nothing, while a missing one is a silent retry storm the day
    # somebody schedules this.
    "ErasureError",
    # A vector store that cannot be *built* as configured: the client package is not installed, or
    # the provider names no adapter. Not `VectorStoreError`, which is the store being unreachable —
    # that one is a `SubsystemUnavailableError` and must stay retryable, since the identical call
    # succeeds once the store is back. No retry installs a package.
    "VectorStoreConfigError",
    # The prescriptive-design tier (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`).
    # All three are facts about the request rather than about the attempt, so the identical call
    # fails identically: a plate that cannot hold the arms holds no more of them on a retry, a
    # design id nothing answers to answers to nothing on a retry, and a revision derived from a
    # stale head is *still* stale — the fix there is a re-read and a re-apply by whoever wrote it,
    # which is the one thing a retry does not do. `RevisionConflict` is the entry worth pausing on:
    # a conflict looks transient and is not, because retrying it would resolve the race by
    # discarding the revision it did not see, which is the whole thing the parent check prevents.
    # Listed even though no workflow writes a design today — the list is keyed by class *name*, so
    # an entry that never matches costs nothing while a missing one is a silent retry storm the day
    # somebody schedules a drafting job.
    #
    # `StatusConflict` joins it on the same reasoning and for a sharper reason. A stale
    # `expected_status` is stale on every attempt, and retrying would resolve the race by
    # discarding the decision it did not see — which is exactly the sign-off the compare-and-set
    # was added to protect. A retried `abandoned` that silently overwrites somebody's `approved`
    # is the defect, not the recovery.
    "LayoutError",
    "RevisionConflict",
    "StatusConflict",
    "UnstorableDocument",
    "UnknownDesign",
    "TemplateError",
    "UnresolvedReference",
    "ProfileError",
    # A BoFire/botorch surrogate fit or acquisition step failed on the given observations
    # (Science-4, `chemclaw.science.bo.engine`). Deterministic in the data: the same duplicate
    # or degenerate points collapse the same kernel on a retry, so this is bad-data, not transient.
    "SurrogateFitError",
    # The four ways a declaratively-bound warehouse source fails (`chemclaw.ingest.eln.warehouse`),
    # all of them deterministic in something a retry cannot change. `BindingError`/`PathSyntaxError`
    # are a malformed binding — the manifest is the same file on the next attempt. `TransformError`
    # is a row carrying a value the binding's vocabulary does not cover; `WarehouseQueryError` is a
    # relation or column the site does not have. An unreachable warehouse is deliberately *not*
    # here: the driver raises `ConnectionError` for that, precisely so it stays retryable.
    "BindingError",
    "PathSyntaxError",
    "TransformError",
    "WarehouseQueryError",
    # A vendored dataset that is absent, malformed, or does not match its manifest checksum
    # (D-135). Emphatically not transient: a retry re-reads the same bytes from the same image
    # layer and reaches the same conclusion, and the fix is a rebuild.
    "VendoredDatasetError",
    # A mounted document share that cannot be read as declared (`chemclaw.ingest.documents`): a
    # malformed binding, or a mount point that is not a directory. Both are the same on the next
    # attempt — a volume that failed to mount does not mount itself because Temporal asked twice —
    # and retrying only delays the log line naming which one it is.
    "DocumentShareError",
    # The calculation server was reached and refused (`chemclaw.connectors.calc.remote`): an
    # unparameterised solvent, an atom index past the molecule, a SMILES outside a predictor's
    # domain. Its sibling `CalcServerError` is deliberately **not** here — an unreachable server is
    # a `SubsystemUnavailableError`, the one fault a retry actually fixes, and conflating the two
    # is what would burn `activity_max_attempts` on a refusal that never changes.
    "CalcToolError",
    # A turn asked a tool the identical question once too often (`chemclaw.agent.repeat_guard`).
    # It never crosses an activity boundary today — the guard is a chat-side middleware — but it is
    # a `ChemclawError`, and the rule this list encodes is that every one of them fails fast: an
    # identical call is identical on the retry too, so retrying is the one thing that cannot help.
    "RepeatedCallRefusal",
    # `AuthorizationError` (`chemclaw.agent.authz`) and its subclasses are NOT `ChemclawError`/
    # `ValueError` — an authorization refusal is a policy decision, not bad data, and reparenting it
    # would make `chemclaw.agent.tool_authz.surface_domain_errors` swallow it ahead of
    # `surface_authorization_denials` (see the class docstring). They are listed here by their own
    # exact names instead: `chemclaw.durable.template_activities.authorize_job_step` raises
    # `AuthorizationError` crossing a real activity boundary, and a refusal never changes on retry,
    # so it must still fail fast there. `tests/test_publish.py` walks this hierarchy the same way it
    # walks `ChemclawError`'s so a future subclass cannot go unregistered unnoticed.
    "AuthorizationError",
    "DryRunRefusal",
    "PlanNotApprovedError",
    # An `agent` step's model reached for a write the template did not declare
    # (`chemclaw.agent.tool_authz`). It is caught and converted inside the turn, so it does not
    # normally cross a boundary — but a template step *is* an activity, and what the step declares
    # is pinned in the run's input, so the identical attempt is refused identically on every
    # attempt. Listed for the same reason every entry here is listed: by class name, and a name
    # that is never matched costs nothing.
    "UndeclaredWriteRefusal",
    # NOT here, deliberately: `SubsystemUnavailableError` (`chemclaw.core.errors`). It reads like
    # a sibling of the two entries above — a non-`ChemclawError` that crosses an activity boundary
    # (a connector-job tool invoked inside `durable.template_activities`) — but it means the
    # opposite thing. An unreachable broker is *retryable*: the identical call succeeds once the
    # subsystem is back, so listing it would make a workflow give up on a broker restart it would
    # otherwise ride out. `tests/test_publish.py` asserts its absence, with the reason.
]

# Bad data is non-retryable by type; `maximum_attempts` bounds the *transient* retries
# so an unclassified deterministic failure (e.g. a `KeyError`/`RuntimeError` bug, or a
# git ref that can never be created) gives up instead of pinning a worker forever.
BAD_DATA_RETRY = RetryPolicy(
    maximum_attempts=settings.activity_max_attempts,
    non_retryable_error_types=list(_BAD_DATA_TYPES),
)


def note_publish_retry() -> RetryPolicy:
    """Bounded retries for a PR-gate note write (config `note_write_max_attempts`).

    Shares the bad-data type list so a bad note (`NoteError`, `ValidationError`) or a structural
    gate refusal (`GitSubmitError` — a mis-pointed checkout, a proposal branch a human pushed to)
    fails fast instead of burning the transient-retry budget. `GitRemoteError` — a dead remote, a
    timed-out command, a contended lock — is the retryable subclass: Temporal matches these names
    exactly, so the subclass's different name is what makes `note_write_max_attempts` real. This
    docstring used to promise that split while the code listed the one class that covered both,
    so a 30-second network blip dropped a note from a synthesis batch on its first attempt.
    """
    return RetryPolicy(
        maximum_attempts=settings.note_write_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def agent_step_retry() -> RetryPolicy:
    """A narrow outer bound for the one activity whose retry is not free.

    Config `agent_step_max_attempts`.

    Every other activity is safe to retry: it recomputes, and recomputing costs time. A template's
    **agent** step is not, because a Temporal retry replays the turn from the prompt — an activity
    has no checkpointer behind it — so every tool the failed attempt already ran runs again with
    its side effects. Measured: one provider 503 produced two PR-gate branches and two audit rows
    for one logical note.

    Same bad-data type list as every policy here, deliberately and without exception: an outer
    bound is a bound on *transient* retries, and which failures are transient is one classification
    that must not depend on which activity asked. In particular a provider 503 stays **retryable**
    — `SubsystemUnavailableError` and the provider SDKs' own exception names are absent from
    `_BAD_DATA_TYPES` on purpose (`tests/test_publish.py` asserts the absence). Filing a 503 as bad
    data would be false in exactly the direction that list exists to keep straight, and Temporal
    matches by bare class name, which `anthropic` and `openai` share. The right lever is how many
    attempts, not what kind of failure it was.

    **The accepted cost, named rather than discovered:** the retry that helps already happens
    inside the SDK (`llm_max_retries=3` is 4 HTTP attempts), so a blip is still ridden out — but a
    *long* provider outage now fails the step in ~4 attempts instead of riding it out over 20
    (4 × `activity_max_attempts`). A cleanly failed run that a person re-runs costs less than
    duplicate notes and duplicate audit rows that a person has to find and reconcile.
    """
    return RetryPolicy(
        maximum_attempts=settings.agent_step_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
    )


def queue_wait_timeout() -> timedelta:
    """How long a core activity may sit unclaimed on its queue, as `schedule_to_start_timeout`.

    **`start_to_close_timeout` is not a bound on a call, and reading it as one left every durable
    job here able to hang forever.** It starts counting when a worker *picks the task up*, so a
    queue nobody polls — the background fleet scaled to zero, a rolling update, a queue named in
    config but served by no pod — is an activity that never times out and a workflow that never
    ends. Most of these workflows are Temporal Schedules under `ScheduleOverlapPolicy.SKIP`, so one
    wedged run then skips every subsequent fire of that job family, indefinitely, with no error
    anywhere. `durable/notify.py` measured the shape on one call (a workflow still RUNNING after
    75 s against a 30 s start-to-close); it is a property of the timeout rather than of that call,
    which is why the bound is stated once here and passed at every call site.

    **Schedule-to-start rather than schedule-to-close, and the difference was measured**: a
    ScheduleToStart timeout is not retried (against the test server, `maximum_attempts=3` and a 10 s
    bound on an unserved queue failed once, at 10.028 s), while `schedule_to_close_timeout` caps
    every attempt *together* — generalising the two small writes' tighter bound would therefore
    have deleted the retry budget at 31 call sites, silently. Those two keep a tighter bound of
    their own; it is now `light_write_queue_wait_timeout` below rather than a schedule-to-close.

    A function, not a module constant, so the setting is read when the workflow runs rather than
    when the module is imported.

    Returns:
        The `schedule_to_start_timeout` every core activity call passes.
    """
    return timedelta(seconds=settings.activity_queue_wait_seconds)


def light_write_queue_wait_timeout() -> timedelta:
    """How long a *small* write may wait on the shared background queue, before it is a fault.

    Two calls want this rather than the hour above, and both sit at the end of a job: the session
    push-back (`durable/notify.py`) and the durable job record (`durable/connector_job.py`). Both
    are swallowed by their caller, and the job record additionally sits *in front of* the message
    telling a chemist their job died — so an hour of patience there is an hour in which a failed
    job is not reported (`tests/test_durable_observability.py` holds exactly that).

    **They were bounded by `schedule_to_close_timeout` at twice their own work budget — 60 s — and
    that is a total rather than a wait, so it was spent almost entirely on a queue neither call
    controls.** `background-jobs` carries 900 s template agent steps, 300 s report sections and the
    hourly sweeps across eight slots. Measured on the real broker: a 50 ms activity behind a full
    slate waited 41.6 s, and the shipped shape was dropped at 60.1 s with `Activity task timed
    out`; at target load the expected wait for a slot is ~150 s, so essentially every push-back and
    every `job_records` row was lost. Splitting the two quantities — this bounds the wait, the
    caller's own `start_to_close_timeout` bounds the work — also gives those attempts their retry
    budget back, since schedule-to-close had capped all of them together.

    **The number is the longest single activity this queue runs**, `template_step_timeout_seconds`
    (a whole LLM turn as one activity). That is the worst case one holder can put in front of a
    small write, it is 6x the measured expected wait, and it is a twelfth of core's hour — which is
    the bound this exists to be tighter than. Derived rather than configured for
    `durable/heartbeat.py::_HEARTBEATS_PER_TIMEOUT`'s reason: a second knob is a second number to
    keep in step with the first, and the relationship is what has to hold.

    Returns:
        The `schedule_to_start_timeout` the two end-of-job writes pass.
    """
    return timedelta(seconds=settings.template_step_timeout_seconds)


# What fraction of a connector job's own execution budget its activity may spend *waiting for a
# slot*, before the wait is called a fault rather than backpressure. A ratio rather than a setting,
# and derived rather than invented, for `durable/heartbeat.py::_HEARTBEATS_PER_TIMEOUT`'s reason:
# the quantity that matters is the relationship, and two independently configured numbers can drift
# apart into a bound that is either meaningless or fires on healthy load.
#
# **Half, because both halves have to be true at once.** Measured on the real broker at target load
# (200 jobs, 8 slots, linear in activity duration), the `connector-calc` queue's wait is p50 ~1.04 h
# and p95 ~1.98 h — genuine backpressure, and failing that would be a worse defect than the one
# being fixed. Half of `connector_job_timeout_seconds` is 2.5 h at the shipped default: above that
# p95, and strictly below the parent ceiling, which is what makes the failure *say* what happened
# instead of arriving as a bare `WorkflowExecutionTimedOut` five hours later.
_CONNECTOR_QUEUE_WAIT_FRACTION = 0.5


def connector_queue_wait_timeout() -> timedelta:
    """How long a **connector bundle's** activity may sit unclaimed on its own queue.

    `queue_wait_timeout` above is core's, and it is deliberately not this one.
    `D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait` scoped that rule to `durable/`
    and argued the exclusion: on a bundle queue a wait genuinely is backpressure, since a CREST
    search holds its slot for hours and the next one behind it is working as designed. That
    argument is still right, and it is *not* an argument for no bound at all — which is what the
    three bundles shipped. Measured at 200 users, a queued connector job's only ceiling was the
    child's `connector_job_timeout_seconds`, so a job that never got a slot told the chemist
    "running" for up to five hours and then failed as a workflow execution timeout, which is
    delivered to nobody and names neither the queue nor the reason.

    So the bound is the same mechanism at a different scale: generous enough that measured
    backpressure passes through it, tight enough that "no worker is serving `connector-calc`" stops
    being indistinguishable from "every worker is busy". It is derived from the job's own budget
    (`_CONNECTOR_QUEUE_WAIT_FRACTION`) rather than from core's hour, because the thing it must stay
    below is that budget and nothing else.

    Not retried, which is the behaviour wanted: a ScheduleToStart expiry means the queue is
    unserved, and asking the same absent worker again finds the same absence (measured in
    `tests/test_activity_queue_bound.py`).

    Returns:
        The `schedule_to_start_timeout` every connector-bundle activity call passes.
    """
    return timedelta(
        seconds=settings.connector_job_timeout_seconds * _CONNECTOR_QUEUE_WAIT_FRACTION
    )


# How far *down* the first capacity retry may be moved, as a fraction of it. A quarter, which
# spreads a burst of jobs refused together across ~28 s at the shipped 112.5 s first interval —
# comfortably wider than the pod's own refusal latency (measured 49-698 ms) and far short of
# collapsing the schedule. Downward only; `calculation_retry` says why.
_CAPACITY_RETRY_JITTER = 0.25


def calculation_retry() -> RetryPolicy:
    """The retry discipline for an activity that calls the shared calculation backend.

    `BAD_DATA_RETRY`'s type list unchanged — a bad molecule must still fail fast — and its attempt
    count unchanged. What differs is the *spacing*, and it exists because `CalcBusyError` made a
    new kind of failure retryable: the backend refusing because every calculation slot is taken.

    **Temporal's default backoff cannot serve that.** It starts at one second and doubles, so five
    attempts are spent inside fifteen seconds — against a hold that is a whole calculation long (a
    measured CREST search is ~19 minutes at 33 atoms, and the server's own ceiling is four hours).
    Retrying a full pod five times in fifteen seconds is not backpressure, it is a small storm that
    then fails anyway, which would have made the classification fix look like it did nothing.

    **Both ends come from configured values, so the schedule cannot drift from what it is about.**
    A slot frees when a calculation finishes, and the longest single calculation this client will
    wait for is `calc_server_timeout_seconds` — so that is the cap on one interval, since sleeping
    longer than the event being waited for is sleeping past it. The first interval is the cap
    divided by the doublings the attempt budget allows, so raising `activity_max_attempts` buys
    finer retries early rather than a longer tail alone. At the shipped defaults (900 s, 5
    attempts) that is 112.5 s, 225 s, 450 s, 900 s — ~28 minutes of patience, which covers the
    measured search, spent in four wakeups rather than in a spin.

    **It fits inside the parent ceiling, and that is checked arithmetic rather than a hope.** A
    saturation refusal costs milliseconds, so the retries add ~1,688 s to a job whose parent
    execution budget (`connector_job_timeout_seconds`, 18,000 s) already carries 3,000 s of slack
    over one full attempt (`xtb_job_timeout_seconds`, 15,000 s). If a deployment narrows that slack
    the parent ceiling is still the backstop, and `Settings` already refuses a ceiling that does not
    cover one attempt.

    **The jitter is this function's, because Temporal has none — measured rather than assumed.**
    `RetryPolicy` carries no jitter field, and driven against the real broker on 2026-09-05 an
    activity with initial 1 s and coefficient 2 was retried at gaps of 1.016 / 2.013 / 4.015 /
    8.021 s: the schedule is exact. That matters here because the arrival pattern this system is
    sized against is a *burst* — a shared work rhythm, a Monday morning — so a slate of jobs refused
    in the same instant would come back in the same instant, take four of them, and refuse the rest
    again in lockstep. Freed slots then idle between pulses instead of being taken as they open.
    `workflow.random()` is the SDK's per-run deterministic RNG, so a replay reproduces the schedule
    the run already had while two runs get different ones, which is exactly the property wanted.
    Outside a workflow there is no run to desynchronise and no determinism to keep, so the nominal
    schedule is returned — the only caller there is a test reading it.

    Returns:
        The retry policy every activity that dispatches to the calculation backend passes.
    """
    cap = settings.calc_server_timeout_seconds
    # `attempts - 2` because N attempts are N-1 retries, and the *last* of those is the one
    # that should wait a whole calculation: intervals i, 2i, 4i, 8i with 8i == cap.
    doublings = 2 ** max(settings.activity_max_attempts - 2, 0)
    first = cap / doublings
    if workflow.in_workflow():
        # Downward only: jittering upward would push the last interval past `maximum_interval`,
        # where the cap silently swallows it and the spread disappears at exactly the attempt that
        # waits longest.
        first *= workflow.random().uniform(1.0 - _CAPACITY_RETRY_JITTER, 1.0)
    return RetryPolicy(
        maximum_attempts=settings.activity_max_attempts,
        non_retryable_error_types=list(_BAD_DATA_TYPES),
        initial_interval=timedelta(seconds=first),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=cap),
    )


def activity_failure_reason(exc: ActivityError) -> str:
    """A short reason for a *swallowed* activity failure, so one log line separates two states.

    Both best-effort writes in this layer — the session push-back and the durable job record — end
    in an `except ActivityError` that logs and carries on, and both logged the same sentence
    whatever had happened. That is the wrong resolution for the failure they actually see under
    load: a `SCHEDULE_TO_START` expiry is *nobody polling the queue*, which no redelivery and no
    retry can help and which an operator fixes with a worker, while every other failure is the
    write itself. Reported by type otherwise, which is strictly more than the line said before.

    Args:
        exc: The swallowed activity error, whose `cause` carries what Temporal decided.

    Returns:
        A sentence fragment for the caller's own log line. Never raises: a best-effort path must
        not acquire a new way to fail while explaining one.
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

    For workflows whose real result is the calculation, not the note (QM, BO):
    the science is done and cached, so a broken git remote must not fail the job.

    Swallowing is right for the *job* and was wrong for the *knowledge*. A warning inside a
    workflow log is not something anyone watches, and `chemclaw_notes_proposed_total` counts only
    successes — so a dead git remote produced no proposals and no signal, which is byte-for-byte
    what an idle deployment produces. The counter below is the difference between those two states.
    Guarded on `is_replaying` for the same reason Temporal's own workflow logger is: a replayed
    history would otherwise re-count every failure the workflow has ever seen.
    """
    try:
        await publish_note(activity, args)
    except ActivityError:
        workflow.logger.warning("knowledge-note publish failed for %s", label)
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_notes_publish_failures_total"))


async def publish_result_best_effort(activity: Any, args: list[Any], label: str) -> None:
    """Queue a finished run's result for the external results store, never failing the caller.

    The same polarity as `publish_note_best_effort` one function up, and for the same reason: by
    the time this runs the scientific result is already durable in `job_records`, so a results
    store — or the local outbox — being unavailable must not fail a completed job and send an
    expensive campaign back round the retry loop.

    It is a *separate* function rather than a parameterization of the note publish, because the two
    differ in every respect that matters: a different timeout (a local enqueue, not a git push), a
    different counter, and a different meaning when it fails. Sharing them would mean one call site
    passing three arguments to say which of two things it is.

    Guarded on `is_replaying` for the counter, exactly as the note publish is: a replayed history
    would otherwise re-count every failure the workflow has ever seen.
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
