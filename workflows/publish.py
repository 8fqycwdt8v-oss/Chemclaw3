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
    from chemclaw.config import settings

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
    """
    try:
        await publish_note(activity, args)
    except ActivityError:
        workflow.logger.warning("knowledge-note publish failed for %s", label)
