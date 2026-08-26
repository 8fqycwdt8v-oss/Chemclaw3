"""The background service that keeps the reaction-label index complete.

Every reaction corpus in this tree — the ELN drops, the Snowflake ELN, the patent corpus — lands a
*record* phase in `reaction_labels` when it is ingested, and nothing else. This job fills in the
rest: the atom map, the named reaction, the per-species roles and structure features. It finds the
work by asking, never by being told: a row whose `labeller_version` differs from the current one is
stale, so a fresh corpus, a re-recorded reaction and an upgraded labeller all produce work through
the same `WHERE` clause and none of them requires anyone to remember anything.

Modelled on `document_sync.py`, whose shape this needs exactly: a planning activity that reads the
live values once, a bounded batch per activity, and `continue_as_new` so a multi-million-row
backlog drains over many runs without an event history that cannot be replayed.

**`version` and `max_iterations` are read in the planning activity, not in workflow code.** Both
decide how many commands the run emits, so reading them live makes the command count a function of
the replaying worker's configuration rather than of history: a redeploy mid-drain then replays
`continue_as_new` at a different point, which is a non-determinism error, which is a workflow *task*
failure, which retries forever and wedges the run (D-093). For `version` the consequence is worse
than a wedge — a labeller upgraded mid-drain would shift the stale set under the loop, so the same
rows would be selected differently on replay.
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
from chemclaw.durable.publish import BAD_DATA_RETRY

# Module-level indirections so tests swap the production index and server client for fakes — the
# shape `eln_sync.py` uses for the same reason.
_label_index = default_label_index
_labeller = RxnLabelServer


def label_policies() -> dict[str, LabelPolicy]:
    """Every enabled source that declares a `labels:` block, by name.

    Also what `durable/schedules.py` asks to decide whether this job earns a Schedule at all. That
    is deliberately not a `labels_enabled` setting: `CHEMCLAW_DATA_SOURCES` plus a declared block
    already answers the question, and a second flag could only restate it or contradict it — the
    argument `core/config/sources.py` makes, and the shape `share_sources()` already has.
    """
    return {m.name: m.labels for m in active_manifests() if m.labels is not None}


class LabelSyncPlan(BaseModel):
    """The two live values one drain is fixed to, read once and recorded in history."""

    # Asked of the labelling server, never derived here — see `ingest/labels/labeller.py`. Half of
    # what decides whether a row is stale, so a locally-built one would be well-formed and match
    # nothing: every row would look stale forever and the drain would never converge.
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

    Both are live reads that belong in an activity, and neither may be re-read by a replaying
    worker — see the module docstring.
    """
    return LabelSyncPlan(
        version=await _labeller().version(),
        max_iterations=settings.label_sync_max_iterations,
    )


# One batch is `label_batch_size` reactions through an atom-mapping transformer — minutes of remote
# work with no natural progress point to report — so liveness is time-based: `beating` beats while
# the batch runs, and Temporal detects a dead worker within the heartbeat timeout instead of waiting
# out the whole start-to-close. The eager pre-beat is kept because `beating()` waits one interval
# before its first, and a small batch may finish before that.
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
# `failure_exception_types` because without it this workflow cannot fail — it *hangs*. The SDK
# parks a plain exception in an infinite workflow-task-failure loop, so a genuine bad-data failure
# would look like a run that is still going, forever (measured; `connector_job.py` records it).
@workflow.defn(failure_exception_types=[Exception])
class ReactionLabelWorkflow:
    """Drain the reaction-label index's stale rows until none remain or the run's bound is spent.

    Keeps no cursor between runs, for the same reason `DocumentShareSyncWorkflow` does not: the
    stale set *is* the cursor. A row leaves it by being stamped, so the next run picks up exactly
    where this one stopped without anything being written down — and a re-recorded reaction or an
    upgraded labeller puts rows back into it, which a stored position could not express.
    """

    @workflow.run
    async def run(self, state: LabelSyncState | None = None) -> LabelSyncOutcome:
        """Label batches until the index reports no more stale rows.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        timeout = timedelta(seconds=settings.label_sync_timeout_seconds)
        if state is None:
            plan: LabelSyncPlan = await workflow.execute_activity(
                plan_label_sync, start_to_close_timeout=timeout, retry_policy=BAD_DATA_RETRY
            )
            state = LabelSyncState(version=plan.version, max_iterations=plan.max_iterations)
        iterations = 0
        while True:
            batch: LabelReport = await workflow.execute_activity(
                label_stale_reactions,
                args=[state.version],
                start_to_close_timeout=timeout,
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
