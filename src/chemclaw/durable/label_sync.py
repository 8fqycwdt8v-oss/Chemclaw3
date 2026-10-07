"""The background service that keeps the reaction-label index complete.

Every reaction corpus lands only a record phase in `reaction_labels` on ingest; this job fills in
the atom map, named reaction, species roles and structure features. A row whose
`labeller_version` differs from the current one is stale, so new corpora, re-recorded reactions
and labeller upgrades all produce work through one `WHERE` clause.

Shaped like `document_sync.py`: a planning activity, a bounded batch per activity, and
`continue_as_new` to keep history replayable. `version` and `max_iterations` are read in the
planning activity, because both decide the command stream and a live read would break replay.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.labels.enrich import LabelReport, label_stale
    from chemclaw.ingest.labels.labeller import RxnLabelServer
    from chemclaw.ingest.sources.registry import active_manifests
    from chemclaw.science.labels.policy import LabelPolicy
    from chemclaw.science.labels.store import default_label_index

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Module-level indirections so tests swap the production index and server client for fakes — the
# shape `eln_sync.py` uses for the same reason.
_label_index = default_label_index
_labeller = RxnLabelServer


def label_policies() -> dict[str, LabelPolicy]:
    """Every enabled source that declares a `labels:` block, by name.

    What a source carries, looked up per row; a source absent from the map is labelled under
    `_DERIVE_EVERYTHING`. No separate `labels_enabled` setting: `CHEMCLAW_DATA_SOURCES` plus a
    declared block already answers it.
    """
    return {m.name: m.labels for m in active_manifests() if m.labels is not None}


class LabelSyncPlan(BaseModel):
    """The two live values one drain is fixed to, read once and recorded in history."""

    # Asked of the labelling server, never derived here: a locally-built version would match no row,
    # so every row would look stale forever.
    version: str
    max_iterations: int


class LabelSyncOutcome(BaseModel):
    """What one run did, in the two numbers an operator needs to tell working from broken."""

    labelled: int = 0
    # Rows stamped with nothing derived. Reported separately because a run that stamps thousands
    # and derives none is a broken labeller reporting healthy progress, and one total cannot say so.
    unlabelled: int = 0


class LabelSyncState(BaseModel):
    """A run's position, carried across `continue_as_new` so a huge corpus drains over many runs."""

    version: str
    max_iterations: int
    labelled: int = 0
    unlabelled: int = 0


@durable_activity("background")
@activity.defn
async def plan_label_sync() -> LabelSyncPlan:
    """Ask the server what version it is, and fix the run's iteration bound.

    Both are live reads that a replaying worker must not redo — see the module docstring.
    """
    return LabelSyncPlan(
        version=await _labeller().version(),
        max_iterations=settings.label_sync_max_iterations,
    )


# A batch is minutes of remote atom mapping with no progress point, so liveness is time-based via
# `beating`; the eager pre-beat covers a batch shorter than one interval.
@durable_activity("background")
@activity.defn
async def label_stale_reactions(version: str) -> LabelReport:
    """Label one bounded batch of rows that are stale at `version`, and stamp them."""
    activity.heartbeat()
    return await beating(
        label_stale(
            _label_index(),
            _labeller(),
            label_policies(),
            version,
            settings.label_batch_size,
        ),
        "reaction labelling",
        settings.label_sync_heartbeat_timeout_seconds,
    )


@durable_workflow("background")
# Without `failure_exception_types` a bad-data failure would park in an infinite workflow-task
# retry loop and look like a run still going.
@workflow.defn(failure_exception_types=[Exception])
class ReactionLabelWorkflow:
    """Drain the reaction-label index's stale rows until none remain or the run's bound is spent.

    Keeps no cursor between runs: the stale set is the cursor, and re-recorded reactions or an
    upgraded labeller put rows back into it.
    """

    @workflow.run
    async def run(self, state: LabelSyncState | None = None) -> LabelSyncOutcome:
        """Label batches until the index reports no more stale rows.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        timeout = timedelta(seconds=settings.label_sync_timeout_seconds)
        if state is None:
            plan: LabelSyncPlan = await workflow.execute_activity(
                plan_label_sync,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state = LabelSyncState(version=plan.version, max_iterations=plan.max_iterations)
        iterations = 0
        while True:
            batch: LabelReport = await workflow.execute_activity(
                label_stale_reactions,
                args=[state.version],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=timedelta(seconds=settings.label_sync_heartbeat_timeout_seconds),
                # Bad data is dropped per reaction inside the pass; what reaches here is a refused
                # request the whole batch shares, which no retry changes.
                retry_policy=BAD_DATA_RETRY,
            )
            state.labelled += batch.labelled
            state.unlabelled += batch.unlabelled
            iterations += 1
            if not batch.has_more:
                break
            if iterations >= state.max_iterations:
                # The state carried forward is two counters and two strings, so unlike the document
                # drain there is nothing to compact: the payload cannot grow with the corpus.
                workflow.continue_as_new(state)
        return LabelSyncOutcome(labelled=state.labelled, unlabelled=state.unlabelled)
