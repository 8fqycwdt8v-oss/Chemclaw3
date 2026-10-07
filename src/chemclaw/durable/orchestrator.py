"""Generic child-workflow fan-out: run N independent sub-tasks as child workflows.

`fan_out` runs each input as its own child workflow with bounded concurrency and per-child
isolation, so one poison item cannot fail the batch. A child that exhausts its retries is logged
and dropped (D-030); the rest return in input order. Inputs pass to children verbatim, so an
input carrying `requested_by` propagates that actor.
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

    The batch size decides how many child starts each workflow task emits, so it is recorded in
    history once through a local activity rather than read live.
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

    The retry policy bounds failures; the cap bounds a child that neither fails nor completes, which
    would otherwise be awaited forever.
    """
    return await workflow.execute_child_workflow(
        child.run,
        payload,
        id=f"{parent_id}-{id_prefix}-{index}",
        task_queue=task_queue,
        retry_policy=retry_policy,
        execution_timeout=execution_timeout,
    )


def _refuse_a_child_that_cannot_fail(child: Any) -> None:
    """Refuse a `fan_out` child that has not declared how it fails, at the seam that depends on it.

    A child raising a plain exception without `failure_exception_types` parks in the SDK's task
    retry loop until its execution timeout, logged identically to a hung child. Checked over the
    class passed in, so any caller is covered. A `child` with no
    `__temporal_workflow_definition` (a test double) is left alone.
    """
    definition = getattr(child, "__temporal_workflow_definition", None)
    if definition is None:
        return
    if not getattr(definition, "failure_exception_types", ()):
        raise ValueError(
            f"{getattr(definition, 'name', child)!r} is passed to fan_out without "
            "failure_exception_types, so a plain exception raised in it would park in the SDK's "
            "task-failure loop rather than fail: the fan-out could not drop it, and it would cost "
            "the full fan_out_child_timeout_seconds and log as a timeout. Declare "
            "@workflow.defn(failure_exception_types=[...]) on it."
        )


async def fan_out(
    child: Any,
    inputs: Sequence[Any],
    *,
    id_prefix: str,
    max_parallel: int | None = None,
) -> list[Any]:
    """Run each of `inputs` as a `child` workflow, bounded-parallel, returning successful results.

    `child` must be able to fail for isolation to work (D-093): a child raising a plain exception
    needs `@workflow.defn(failure_exception_types=[...])`, enforced by
    `_refuse_a_child_that_cannot_fail`. A child whose failures are already SDK `FailureError`s is
    fine as-is.

    Args:
        child: The child workflow class to start (its `run` method is invoked with one input).
        inputs: One payload per child, run in input order; each must be serializable by the pydantic
            data converter (a pydantic model or scalar).
        id_prefix: A short, caller-chosen tag for the child ids (`<parent>-<prefix>-<i>`), so a
            child in the Temporal UI reads as e.g. `...-section-2`. Required — ids must be clear.
        max_parallel: Concurrency bound; defaults to `orchestrator_max_parallel_children`,
            resolved via a local activity so replay stays deterministic across config changes.

    Returns:
        The results of the children that succeeded, in input order. A child that fails after its
        retries is logged and omitted (D-030: reject-and-continue), never restarting its siblings.
    """
    # Read once so every child of one fan-out gets the same bound.
    child_timeout = timedelta(seconds=settings.fan_out_child_timeout_seconds)
    if max_parallel is not None:
        limit = max_parallel
    else:
        limit = await workflow.execute_local_activity(
            resolve_fan_out_limit,
            # The generic short-activity budget (same knob the notify seam uses for its write).
            start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
    if limit < 1:
        raise ValueError(f"max_parallel must be >= 1, got {limit}")
    _refuse_a_child_that_cannot_fail(child)
    parent_id = workflow.info().workflow_id
    indexed = list(enumerate(inputs))
    results: list[Any] = []
    # Batches rather than a semaphore: deterministic under replay, same concurrency bound.
    #
    # Children run on core's `background-jobs` queue under `BAD_DATA_RETRY` (bounded; Temporal's
    # default retries forever). Only its `maximum_attempts` takes effect here, since a child failing
    # through its activity surfaces as a child failure; acceptable because fan-out children are
    # small
    # and independent.
    for batch in _batches(indexed, limit):
        settled = await asyncio.gather(
            *(
                _run_child(
                    child,
                    index,
                    payload,
                    id_prefix=id_prefix,
                    parent_id=parent_id,
                    task_queue=settings.background_task_queue,
                    retry_policy=BAD_DATA_RETRY,
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
                # Counted as well as logged, because the parent completes successfully with a short
                # list; guarded
                # on `is_replaying` so a replay does not re-count.
                if not workflow.unsafe.is_replaying():
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
