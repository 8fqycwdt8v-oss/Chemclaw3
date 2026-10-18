"""Stop a best-effort per-item loop once its failures say the dependency is down, not the item.

A workflow that swallows each item's failure (one subscriber's mailbox, one source's export) pays
that item's whole retry window for it, every backoff of the item's class. In a hard outage every
item fails that way, so a loop over N items costs N windows, and a scheduled run that outlives
`schedule_run_timeout_seconds` is killed with nothing recorded. `LoopOutage` stops the loop after
`loop_outage_consecutive_failures` such failures in a row; the items it did not reach keep their
watermark or cursor, so the next run resumes them.

Only a failure that spent its retries counts (`_spent_its_retries`): a refusal of one bad item
ends in one attempt and costs no window, and counting it would let a bad item at the head of the
list stop every later run. Nothing is recorded in history until the first such failure, so a
healthy run's command sequence is the one it always was.
"""

from collections.abc import Sequence
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.exceptions import ActivityError, RetryState

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.registry import ActivityClass, durable_activity

from chemclaw.durable.retry import retry_for

# How an activity ends after spending its window: out of attempts, or out of the time its retries
# were allowed. `NON_RETRYABLE_FAILURE` is the one-attempt refusal that must not count.
_SPENT = frozenset({RetryState.MAXIMUM_ATTEMPTS_REACHED, RetryState.TIMEOUT})


@durable_activity("background", ActivityClass.IO)
@activity.defn
async def resolve_loop_outage_failures() -> int:
    """Resolve the consecutive-failure bound outside workflow code, as `resolve_fan_out_limit` does.

    The bound decides how many items a run dispatches, so reading settings in workflow code would
    make the command count depend on the replaying worker's config.
    """
    return settings.loop_outage_consecutive_failures


def _spent_its_retries(failure: BaseException) -> bool:
    """Whether a swallowed failure cost the item its whole retry window rather than one attempt."""
    return isinstance(failure, ActivityError) and failure.retry_state in _SPENT


class LoopOutage:
    """Counts one loop's consecutive spent failures and says when the loop should stop.

    `patch_id` gates the first resolution of the bound with `workflow.patched`, so a run recorded
    before this existed replays with the bound off; the id may never be reused.
    """

    def __init__(self, patch_id: str) -> None:
        """Start with no failures and the bound unresolved, which costs a run no command."""
        self._patch_id = patch_id
        self._run = 0
        self._bound: int | None = None

    async def should_stop(self, failure: BaseException | None) -> bool:
        """Book one item's outcome (`None` for success); whether the loop must stop here.

        A success resets the count; a failure that did not spend its retries leaves it alone,
        since it says nothing about the dependency.
        """
        if failure is None:
            self._run = 0
            return False
        if not _spent_its_retries(failure):
            return False
        self._run += 1
        if self._bound is None:
            self._bound = await self._resolve()
        return 0 < self._bound <= self._run

    async def _resolve(self) -> int:
        """The bound for this run; 0, meaning never stop, for a run recorded before the patch."""
        if not workflow.patched(self._patch_id):
            return 0
        return await workflow.execute_local_activity(
            resolve_loop_outage_failures,
            start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
            retry_policy=retry_for(resolve_loop_outage_failures),
        )

    def stopped(self, skipped: Sequence[str]) -> None:
        """Say that the loop stopped and which items it left for the next run.

        Counted on `chemclaw_loop_outage_stops_total{workflow}` so a run that stopped short is
        distinguishable from a quiet one; guarded so a replay does not count it again. A stop at
        the last item left nothing, so it is not one.
        """
        if not skipped:
            return
        name = workflow.info().workflow_type
        workflow.logger.warning(
            "%s stopped after %d consecutive items each failed after their whole retry window: "
            "the dependency they share is down. %d item(s) not attempted, left for the next run: "
            "%s",
            name,
            self._run,
            len(skipped),
            ", ".join(skipped),
        )
        if not workflow.unsafe.is_replaying():
            record_metric(
                lambda m: m.increment("chemclaw_loop_outage_stops_total", labels={"workflow": name})
            )
