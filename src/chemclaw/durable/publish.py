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
exact class name, so the concrete names are listed too).
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric

# Temporal matches `non_retryable_error_types` by exact class name (not isinstance),
# so every bad-data name that can cross an activity boundary is listed explicitly.
# `ValidationError` (pydantic) subclasses `ValueError` but has its own class name, so
# a model-build failure on corrupt data would otherwise be treated as retryable.
_BAD_DATA_TYPES = [
    "ValueError",
    "ValidationError",
    "ChemclawError",
    "InvalidSmilesError",
    "FingerprintError",
    "ElnMappingError",
    "ElnFormatError",
    "OrdFormatError",
    "IngestError",
    "MetricError",
    "PlaybookError",
    "NoteError",
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
    "ConnectorJobError",
    "GitSubmitError",
    "CalculationDomainError",
    "ConnectorError",
    "DataSourceError",
    # A derived label written for a reaction whose record phase was never stored
    # (`chemclaw.science.labels.store`). Non-retryable because the missing row is a fact about the
    # corpus, not about this attempt: the drain must record the reaction before it can label it,
    # and retrying the same write finds the same absence.
    "LabelIndexError",
    # The labelling server reached and refused: a reaction SMILES RDKit cannot parse, a species
    # list that does not match the reaction (`chemclaw.ingest.labels.labeller`). Its retryable
    # sibling is `LabelServerError`, which means nobody answered at all.
    "LabelToolError",
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

    Shares the bad-data type list so a bad note (`NoteError`, `ValidationError`)
    fails fast instead of burning the transient-retry budget; only a genuinely
    transient `GitSubmitError` (dead remote) is retried, up to the bound.
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


async def publish_note(activity: Any, args: list[Any]) -> str:
    """Run a note-publish activity with the shared queue/timeout/retry discipline."""
    result: str = await workflow.execute_activity(
        activity,
        args=args,
        task_queue=settings.background_task_queue,
        start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
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
