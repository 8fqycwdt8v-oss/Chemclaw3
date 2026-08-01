"""The `qm` connector's durable workflow: one QM/DFT calculation on the HPC cluster.

Deterministic orchestration only: it sequences the activities (prepare → submit → poll → parse) and
owns their timeouts (pulled from `chemclaw.core.config`, never hardcoded). All non-determinism
lives in
`chemclaw.connectors.qm.activities`. Restarting a worker mid-run must resume from event history
without
re-executing a completed activity — the durability spike verified at CHECKMATE 1.

**What it no longer does is the point of D-118's last commit.** This was the one capability whose
durable path was hand-written into core: its own launcher tool, its own status tool, its own queue,
its own worker, and a workflow that published its own graph note and sent its own session push-back.
Those last two are cross-cutting obligations, and `ConnectorJobWorkflow` — now this run's parent —
owns them. So the note is *built* here (the QM→note mapping is this domain's knowledge) and
*published* by core through the PR-gate, and the push-back is core's entirely. What is left is the
chemistry.

The class keeps its name. `@workflow.defn` derives the Temporal type name from `__name__`, so moving
the module is invisible to a recorded history while renaming the class is not — which is exactly why
`docs/guides/workflow-versioning.md` records the `QMJobWorkflow` → `CalculationWorkflow` rename as
dropped rather than deferred.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

# Activities, models, and config are ordinary modules that must bypass the workflow sandbox's
# re-import isolation (the standard Temporal pattern).
with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.qm.activities import (
        lookup_qm_result,
        parse_qm_output,
        persist_qm_result,
        poll_hpc_status,
        prepare_input,
        submit_to_hpc,
    )
    from chemclaw.connectors.qm.knowledge import note_from_qm_result, qm_energy_estimate
    from chemclaw.connectors.qm.specs import QmCacheLookup, QMJobInput, QMJobResult, QmJobSpec
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult

from chemclaw.connectors.queues import bundle_queue
from chemclaw.durable.publish import BAD_DATA_RETRY
from chemclaw.durable.registry import durable_workflow


@durable_workflow(bundle_queue("qm"))
@workflow.defn
class QMJobWorkflow:
    """Run one QM calculation durably and return it in the connector envelope."""

    @workflow.run
    async def run(self, spec: QmJobSpec) -> ConnectorJobResult:
        """Execute the QM job end-to-end; safe to replay and to resume after a worker restart.

        The argument is the bare spec — exactly what the model may author — because
        `ConnectorJobWorkflow` is the parent and already holds the actor and the session. The actor
        still has to reach `submit_to_hpc`, since the cluster run is launched under the shared HPC
        *service* identity and the requesting user is the only thing that makes it attributable
        (F4-T3). It arrives on the run's **memo** rather than on the spec: a memo is per-execution
        metadata Temporal carries beside the argument, so the identity travels without becoming a
        field the LLM could fill in. The default keeps the configured service identity for a run
        started outside the wrapper (tests, a manual re-drive) — the fallback `require_actor` uses.
        """
        job = QMJobInput(
            **spec.model_dump(),
            requested_by=workflow.memo_value("requested_by", settings.service_actor_id),
        )
        activity_timeout = timedelta(seconds=settings.qm_activity_timeout_seconds)

        prepared = await workflow.execute_activity(
            prepare_input, job, start_to_close_timeout=activity_timeout, retry_policy=BAD_DATA_RETRY
        )

        # Compute-once for the most expensive thing the system runs (D-011/D-158). The workflow id
        # already deduplicates identical requests, but only while Temporal retains the execution —
        # once it ages out, the id is free again and the same molecule re-ran hours of cluster time.
        # The store has no such horizon, so this is the lookup that actually makes the rule hold.
        # A lookup failure is not fatal: the worst case is the recompute we would have done anyway.
        try:
            cached = await workflow.execute_activity(
                lookup_qm_result,
                prepared,
                start_to_close_timeout=activity_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "could not read the calculation store for %s; recomputing",
                prepared.molecule_smiles,
                exc_info=True,
            )
            cached = QmCacheLookup()
        if cached.result is not None:
            return _envelope(cached.result, cached.calc_key)

        handle = await workflow.execute_activity(
            submit_to_hpc,
            prepared,
            start_to_close_timeout=activity_timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        # The poll's start-to-close budget must cover the *entire* run in one attempt —
        # heartbeating resets only the heartbeat timeout, never start-to-close. The mock finishes in
        # `hpc_mock_run_seconds`; a real Nextflow run takes far longer, so the two backends use
        # different budgets (F5, review finding: a mock-derived 36s cap would kill every real run).
        if settings.hpc_launch_interface == "nextflow":
            poll_budget = settings.hpc_run_timeout_seconds
            poll_heartbeat = settings.hpc_run_heartbeat_timeout_seconds
        else:
            poll_budget = settings.hpc_mock_run_seconds + settings.qm_activity_timeout_seconds
            poll_heartbeat = settings.qm_poll_heartbeat_timeout_seconds
        raw_output = await workflow.execute_activity(
            poll_hpc_status,
            handle,
            start_to_close_timeout=timedelta(seconds=poll_budget),
            heartbeat_timeout=timedelta(seconds=poll_heartbeat),
            retry_policy=BAD_DATA_RETRY,
        )
        result = await workflow.execute_activity(
            parse_qm_output,
            args=[prepared, raw_output],
            start_to_close_timeout=activity_timeout,
            retry_policy=BAD_DATA_RETRY,
        )

        # Persist the number itself (D-158), so an hours-long run survives independently of whether
        # a human merges the note's PR and of how long Temporal retains this execution. Failure is
        # absorbed rather than fatal: Temporal has already retried the activity `activity_max_
        # attempts` times by the time this raises, so what is left is a persistently unreachable
        # store — and losing the cache entry is worth far less than the completed science, which is
        # still returned and still published. The empty key degrades the note to today's shape.
        try:
            calc_key = await workflow.execute_activity(
                persist_qm_result,
                result,
                start_to_close_timeout=activity_timeout,
                retry_policy=BAD_DATA_RETRY,
            )
        except ActivityError:
            # WARNING, not ERROR: the job succeeded. This is the cache write behind it failing,
            # which costs a recompute on the next identical request — the exact regression D-158
            # exists to prevent, so it must be visible rather than silent.
            workflow.logger.warning(
                "could not persist the QM result for %s; the job stands but its calculation "
                "store entry and the note's calc_refs are missing",
                result.molecule_smiles,
                exc_info=True,
            )
            calc_key = ""

        return _envelope(result, calc_key)


def _envelope(result: QMJobResult, calc_key: str) -> ConnectorJobResult:
    """Wrap a finished result in the envelope core reads — the one exit both paths take.

    The note is *built* here and published by core (step 2.8): the QM→note mapping is this domain's
    knowledge, while the PR-gate is the GxP boundary a connector must not be able to reach around.
    Returned unconditionally — whether it is published is the manifest's `publish_to_graph`, which
    core reads, so this workflow carries no second switch for the same decision.

    Shared by the computed path and the cache-hit path so the two can never answer differently: a
    result served from the store has to look exactly like the run that produced it, including the
    note, or "cached" would become a visible and confusing distinction for the chemist.
    """
    return ConnectorJobResult(
        summary=(
            f"{result.method}/{result.basis_set} on {result.molecule_smiles}: "
            f"{qm_energy_estimate(result).render(fmt='.6f')}"
        ),
        data=result.model_dump(mode="json"),
        note=note_from_qm_result(result, calc_key),
    )
