"""The `bo` connector's own durable workflow: the Bayesian-optimization campaign (plan step 1d.4).

Wraps the ask/tell loop in Temporal so a long campaign is resumable and survives worker restarts:
each round's propose and evaluate are activities, and the observation history is carried as
workflow state (plain data, so replay is deterministic). The best-so-far reduction runs in the
workflow (pure). Objective evaluation is heavy and non-deterministic, hence an activity resolved
by name.

**This is the reference connector-owned workflow** (D-110/D-111), and what it does *not* do is
the point. It returns a `ConnectorJobResult` and stops: core's `ConnectorJobWorkflow` supplies
the idempotent job id, the actor attribution, the session push-back, and the PR-gate publish of
the note this returns. It is served by this bundle's own worker on its own task queue, so
`bofire`/`botorch` run nowhere near the chat service — and the only thing binding it to core is
the workflow *type name* and that queue, both strings in `connector.yaml`.

Moving it here was a one-line manifest change plus this file's return type, which is the
property the seam was built to have.
"""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bo.problem import (
        CampaignResult,
        CampaignSpec,
        Observation,
        best_of,
        discrete_candidate_count,
        space_exhausted,
    )
    from chemclaw.config import settings
    from connectors.bo.activities import (
        evaluate_candidates,
        propose_initial,
        propose_next,
    )
    from connectors.bo.knowledge import note_from_campaign_result
    from workflows.connector_job import ConnectorJobResult

from workflows.publish import BAD_DATA_RETRY


@workflow.defn
class BoCampaignWorkflow:
    """Run a BO campaign durably and return the best point, the history, and the note to gate."""

    @workflow.run
    async def run(self, payload: dict[str, object]) -> ConnectorJobResult:
        """Seed, then run `n_rounds` propose→evaluate rounds, durably.

        Takes the plain mapping core forwards rather than a typed argument: the connector
        contract is payload-in, envelope-out, and the payload has already been validated against
        `CampaignSpec` by the generated tool before the workflow started. Re-validating it here
        is the cheap way to get the typed object back without core needing to know this type.
        """
        spec = CampaignSpec.model_validate(payload)
        timeout = timedelta(seconds=settings.bo_activity_timeout_seconds)

        seed = await workflow.execute_activity(
            propose_initial,
            args=[spec.problem, spec.n_initial, spec.seed],
            start_to_close_timeout=timeout,
            retry_policy=BAD_DATA_RETRY,
        )
        history: list[Observation] = await workflow.execute_activity(
            evaluate_candidates,
            args=[spec.objective_name, seed],
            start_to_close_timeout=timeout,
            retry_policy=BAD_DATA_RETRY,
        )

        space = discrete_candidate_count(spec.problem)
        for _ in range(spec.n_rounds):
            # Stop early if a purely discrete candidate set is exhausted.
            if space_exhausted(space, history, spec.batch):
                break
            proposed = await workflow.execute_activity(
                propose_next,
                args=[spec.problem, history, spec.batch, spec.seed],
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            history += await workflow.execute_activity(
                evaluate_candidates,
                args=[spec.objective_name, proposed],
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
            )

        result = CampaignResult(best=best_of(spec.problem, history), history=history)

        # The recommendation as a PR-gated note (step 1d.5) — *built* here, because the BO→note
        # mapping is this domain's knowledge, and *published* by core, because the PR-gate is
        # the GxP boundary and a connector must not be able to reach around it. Best-effort
        # publishing (a failed git write must never fail a completed campaign) is core's
        # discipline too, so this workflow no longer carries it.
        note = (
            note_from_campaign_result(spec.objective_name, result)
            if spec.publish_to_graph
            else None
        )
        best = result.best
        return ConnectorJobResult(
            summary=(
                f"campaign {spec.objective_name!r} finished after {len(history)} evaluation(s); "
                f"best objective {best.value:.6g} ({best.provenance})"
            ),
            data=result.model_dump(mode="json"),
            note=note,
        )
