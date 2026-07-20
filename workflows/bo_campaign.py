"""Durable Bayesian-optimization campaign workflow (plan step 1d.4).

Wraps the ask/tell loop in Temporal so a long campaign is resumable and survives
worker restarts: each round's propose and evaluate are activities, and the
observation history is carried as workflow state (plain data, so replay is
deterministic). The best-so-far reduction runs in the workflow (pure). Objective
evaluation is heavy and non-deterministic, hence an activity resolved by name.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from bo.problem import (
        CampaignResult,
        CampaignSpec,
        Observation,
        best_of,
        discrete_candidate_count,
        distinct_candidate_count,
    )
    from chemclaw.config import settings
    from workflows.bo_activities import (
        evaluate_candidates,
        propose_initial,
        propose_next,
    )
    from workflows.bo_knowledge import write_campaign_node

# Bad data (an unknown objective name, a malformed problem) will not fix itself on
# retry, so fail fast instead of looping (gate G4).
_RETRY = RetryPolicy(non_retryable_error_types=["ValueError"])


@workflow.defn
class BoCampaignWorkflow:
    """Run a BO campaign durably and return the best point plus the full history."""

    @workflow.run
    async def run(self, spec: CampaignSpec) -> CampaignResult:
        """Seed, then run `n_rounds` propose→evaluate rounds, durably."""
        timeout = timedelta(seconds=settings.bo_activity_timeout_seconds)

        seed = await workflow.execute_activity(
            propose_initial,
            args=[spec.problem, spec.n_initial],
            start_to_close_timeout=timeout,
            retry_policy=_RETRY,
        )
        history: list[Observation] = await workflow.execute_activity(
            evaluate_candidates,
            args=[spec.objective_name, seed],
            start_to_close_timeout=timeout,
            retry_policy=_RETRY,
        )

        space = discrete_candidate_count(spec.problem)
        for _ in range(spec.n_rounds):
            # Stop early if a purely discrete candidate set is exhausted.
            if space is not None and distinct_candidate_count(history) + spec.batch > space:
                break
            proposed = await workflow.execute_activity(
                propose_next,
                args=[spec.problem, history, spec.batch],
                start_to_close_timeout=timeout,
                retry_policy=_RETRY,
            )
            history += await workflow.execute_activity(
                evaluate_candidates,
                args=[spec.objective_name, proposed],
                start_to_close_timeout=timeout,
                retry_policy=_RETRY,
            )

        result = CampaignResult(best=best_of(spec.problem, history), history=history)

        # Optionally publish the recommendation as a PR-gated graph note (step 1d.5),
        # on the light background-jobs queue. Best-effort: a failed git write must not
        # fail the (completed) campaign, so bound the retries and swallow the error.
        if spec.publish_to_graph:
            try:
                await workflow.execute_activity(
                    write_campaign_node,
                    args=[spec.objective_name, result],
                    task_queue=settings.background_task_queue,
                    start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
                    retry_policy=RetryPolicy(maximum_attempts=settings.note_write_max_attempts),
                )
            except ActivityError:
                workflow.logger.warning(
                    "bo-candidate publish failed for objective %s", spec.objective_name
                )

        return result
