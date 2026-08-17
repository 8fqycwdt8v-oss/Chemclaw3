"""Generic child-workflow fan-out (plan F10-D1): run N independent sub-tasks as child workflows.

Orchestration is a Temporal-layer concern (the layer rule: layer 1 stays the single conversational
agent; durability and fan-out live here). `fan_out` runs each input as its own child workflow with
bounded concurrency and per-child isolation, so a report's sections or a memory job's groups each
get independent retry + worker-restart durability instead of one monolithic activity where a single
poison item fails the whole batch. Built with a *second real caller* in hand (the report and memory
workflows both adopt it, D-A13 / Rule of Three), not speculatively.

Isolation follows the D-030 discipline: a child that exhausts its retries is logged and dropped, and
its siblings are unaffected — the fan-out returns the successful results in input order. Identity
flows through unchanged: `fan_out` passes each input to its child verbatim, so an input that carries
`requested_by` (F4-T3, as the QM job inputs do) propagates that actor into the child's audit trail.
"""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.registry import durable_activity

from chemclaw.durable.publish import BAD_DATA_RETRY


@durable_activity("background")
@activity.defn
async def resolve_fan_out_limit() -> int:
    """Resolve the configured fan-out concurrency bound — outside workflow code, on purpose.

    The batch size decides how many StartChildWorkflow commands each workflow task emits, so
    reading live settings *inside* `fan_out` would break replay whenever the config changed
    mid-flight (history recorded N starts, the redeployed worker emits M). Resolving it through
    a (local) activity records the value in history once per fan-out, making the batch shape a
    pure function of history — the deterministic-capture pattern the Temporal SDK prescribes
    for mutable config.
    """
    return settings.orchestrator_max_parallel_children


def _batches(items: list[Any], size: int) -> list[list[Any]]:
    """Split `items` into consecutive batches of at most `size` (order preserved)."""
    return [items[start : start + size] for start in range(0, len(items), size)]


async def _run_child(
    child: Any,
    index: int,
    payload: Any,
    *,
    id_prefix: str,
    parent_id: str,
    task_queue: str,
    retry_policy: RetryPolicy,
    execution_timeout: timedelta,
) -> Any:
    """Start and await one child workflow with a deterministic, unique id, under a wall-clock cap.

    **The retry policy is not that cap, and this call had only a retry policy.** `BAD_DATA_RETRY`
    bounds how many times a child may *fail*; a child that neither fails nor completes — an
    activity blocked on a dead dependency, a heartbeat that stops — is retried zero times and
    awaited forever by the `asyncio.gather` in `fan_out`, which is a fan-out that can never
    isolate-and-drop it. `ConnectorJobWorkflow` bounds its own child exactly this way; these were
    the two starts in the tree with nothing above them.
    """
    return await workflow.execute_child_workflow(
        child.run,
        payload,
        id=f"{parent_id}-{id_prefix}-{index}",
        task_queue=task_queue,
        retry_policy=retry_policy,
        execution_timeout=execution_timeout,
    )


async def fan_out(
    child: Any,
    inputs: Sequence[Any],
    *,
    id_prefix: str,
    task_queue: str | None = None,
    retry_policy: RetryPolicy | None = None,
    max_parallel: int | None = None,
) -> list[Any]:
    """Run each of `inputs` as a `child` workflow, bounded-parallel, returning successful results.

    `child` must actually be able to *fail* for the isolation contract below to mean anything
    (D-093): the Temporal SDK by default treats a raw exception raised in workflow code as a
    possible bug and suspends the workflow via an internal task-failure retry loop that ignores
    `retry_policy` entirely and never gives up, rather than producing a real
    `WorkflowExecutionFailed`. A child whose own failures are already SDK `FailureError`s (e.g. an
    uncaught `ActivityError` from its own `execute_activity`, as in `PublishNoteWorkflow`) is fine
    as-is; a child that raises a plain exception directly needs
    `@workflow.defn(failure_exception_types=[...])` or it will hang instead of being dropped.

    Args:
        child: The child workflow class to start (its `run` method is invoked with one input).
        inputs: One payload per child, run in input order; each must be serializable by the pydantic
            data converter (a pydantic model or scalar).
        id_prefix: A short, caller-chosen tag for the child ids (`<parent>-<prefix>-<i>`), so a
            child in the Temporal UI reads as e.g. `...-section-2`. Required — ids must be clear.
        task_queue: Queue the children run on; defaults to the light `background-jobs` queue.
        retry_policy: Per-child retry policy. None defaults to `BAD_DATA_RETRY` — *not* Temporal's
            own default, which has `maximum_attempts=0` (unlimited) and no non-retryable types, so
            a child that fails deterministically (a bad-data error, or any other exception once its
            own bounded activity retries are exhausted) would retry forever and the fan-out could
            never isolate-and-drop it as documented below (D-093: `_DoublerWorkflow`'s poison input
            hung the fan-out test indefinitely against a real server — the bug this default fixes).
            Pass an explicit policy only when a child genuinely needs a different bound.
        max_parallel: Concurrency bound; defaults to `orchestrator_max_parallel_children`,
            resolved via a local activity so the recorded value — not a live settings read —
            shapes the batches, keeping replay deterministic across config changes.

    Returns:
        The results of the children that succeeded, in input order. A child that fails after its
        retries is logged and omitted (D-030: reject-and-continue), never restarting its siblings.
    """
    queue = task_queue if task_queue is not None else settings.background_task_queue
    # Read here rather than inside `_run_child` so every child of one fan-out is bounded by the
    # same number, whatever a live settings edit does between batches — the determinism reason
    # `max_parallel` is resolved once through a local activity.
    child_timeout = timedelta(seconds=settings.fan_out_child_timeout_seconds)
    # Bounded by default (D-093) — see the `retry_policy` arg doc for why Temporal's own
    # unlimited-retry default would break the isolate-and-drop contract below.
    child_retry_policy = retry_policy if retry_policy is not None else BAD_DATA_RETRY
    if max_parallel is not None:
        limit = max_parallel
    else:
        limit = await workflow.execute_local_activity(
            resolve_fan_out_limit,
            # The generic short-activity budget (same knob the notify seam uses for its write).
            start_to_close_timeout=timedelta(seconds=settings.qm_activity_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
    if limit < 1:
        raise ValueError(f"max_parallel must be >= 1, got {limit}")
    parent_id = workflow.info().workflow_id
    indexed = list(enumerate(inputs))
    results: list[Any] = []
    # Batch rather than a semaphore: a fixed-size batch is deterministic under Temporal's replay
    # (no reliance on lock-acquisition order) and bounds concurrency just the same.
    for batch in _batches(indexed, limit):
        settled = await asyncio.gather(
            *(
                _run_child(
                    child,
                    index,
                    payload,
                    id_prefix=id_prefix,
                    parent_id=parent_id,
                    task_queue=queue,
                    retry_policy=child_retry_policy,
                    execution_timeout=child_timeout,
                )
                for index, payload in batch
            ),
            return_exceptions=True,
        )
        for (index, _payload), outcome in zip(batch, settled, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                # Cancellation is control flow, not a failed child: propagate it (a dropped-and-
                # logged child would silently swallow the cancellation intent).
                raise outcome
            if isinstance(outcome, BaseException):
                # Counted as well as logged, because the parent is about to complete
                # *successfully* with a short list and a log line is not a signal anyone watches.
                # The failure this makes visible: the PR-gate's git credential expires, every
                # child fails, and the memory-synthesis jobs return `[]` every night while
                # `/schedules` shows runs climbing and no failures. `metrics_bridge` is already
                # proven callable from workflow code (`durable/publish.py`).
                record_metric(lambda m: m.increment("chemclaw_fan_out_children_dropped_total"))
                workflow.logger.warning(
                    "fan-out child %s-%s-%d failed and was dropped: %s",
                    parent_id,
                    id_prefix,
                    index,
                    outcome,
                )
            else:
                results.append(outcome)
    return results
