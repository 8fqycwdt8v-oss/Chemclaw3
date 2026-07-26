"""Generic child-workflow fan-out (plan F10-D1): run N independent sub-tasks as child workflows.

Orchestration is a Temporal-layer concern (the layer rule: MAF stays the single conversational
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
    from chemclaw.config import settings

from workflows.publish import BAD_DATA_RETRY


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
    retry_policy: RetryPolicy | None,
) -> Any:
    """Start and await one child workflow with a deterministic, unique id."""
    return await workflow.execute_child_workflow(
        child.run,
        payload,
        id=f"{parent_id}-{id_prefix}-{index}",
        task_queue=task_queue,
        retry_policy=retry_policy,
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

    Args:
        child: The child workflow class to start (its `run` method is invoked with one input).
        inputs: One payload per child, run in input order; each must be serializable by the pydantic
            data converter (a pydantic model or scalar).
        id_prefix: A short, caller-chosen tag for the child ids (`<parent>-<prefix>-<i>`), so a
            child in the Temporal UI reads as e.g. `...-section-2`. Required — ids must be clear.
        task_queue: Queue the children run on; defaults to the light `background-jobs` queue.
        retry_policy: Per-child retry policy (durability + bounded attempts). None uses Temporal's
            default child retry.
        max_parallel: Concurrency bound; defaults to `orchestrator_max_parallel_children`,
            resolved via a local activity so the recorded value — not a live settings read —
            shapes the batches, keeping replay deterministic across config changes.

    Returns:
        The results of the children that succeeded, in input order. A child that fails after its
        retries is logged and omitted (D-030: reject-and-continue), never restarting its siblings.
    """
    queue = task_queue if task_queue is not None else settings.background_task_queue
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
                    retry_policy=retry_policy,
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
