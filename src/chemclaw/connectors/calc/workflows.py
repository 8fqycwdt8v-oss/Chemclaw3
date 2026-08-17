"""The `calc` connector's durable workflow: one expensive calculation, run to completion.

Why the calculators need a durable path at all, when most of this bundle's tools are inline: a
single point is milliseconds and a workflow would be pure overhead, but an optimization plus a
Hessian per species — once per solvent — is minutes. A four-species reaction is seconds; a solvent
screen over five, or a relaxed scan on a mid-sized molecule, is not. That is past what a
conversation can hold open.

**The cost is not predicted, it is measured.** The tool that starts this run waits a bounded moment
for it (`JobSpec.inline_wait_seconds`) and reports the result if it arrives; otherwise the chemist
gets a job id. So the same tool serves the two-second case and the twenty-minute one, and the split
is decided by what actually happened rather than by a cost model that can be wrong in both
directions. The previous design *did* predict, from a power law over the atom count consulted in
the agent's process — which is what kept the whole heavy chemistry closure inside the chat
service's image. That predictor was deleted with its settings once nothing called it (D-117).

Deterministic orchestration only: this sequences and times one activity, and every non-deterministic
thing lives in `chemclaw.connectors.calc.activities`. It runs on this bundle's own queue, reached by
`ConnectorJobWorkflow` under the type name its manifest declares.
"""

from datetime import timedelta

from temporalio import workflow

# Activities, models, and config are ordinary modules that must bypass the workflow sandbox's
# re-import isolation (the standard Temporal pattern).
with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.calc.activities import run_xtb_calculation
    from chemclaw.connectors.calc.specs import XtbJobSpec
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult

from chemclaw.connectors.queues import bundle_queue
from chemclaw.durable.publish import BAD_DATA_RETRY
from chemclaw.durable.registry import durable_workflow


@durable_workflow(bundle_queue("calc"))
# Its failures must be able to *be* failures: without this the SDK parks a plain exception raised
# in workflow code in an unbounded workflow-task-failure loop, so the parent
# `ConnectorJobWorkflow` waits forever and the chemist is told "running" indefinitely. Measured on
# a child reading an absent optional key from its payload (`exclude_none=True` drops one) — child
# RUNNING forever, parent waiting, session never told. See `durable/connector_job.py` for the trade.
@workflow.defn(failure_exception_types=[Exception])
class CalcJobWorkflow:
    """Run one expensive xTB calculation durably and return it in the connector envelope."""

    @workflow.run
    async def run(self, spec: XtbJobSpec) -> ConnectorJobResult:
        """Execute the calculation; safe to replay and to resume after a worker restart.

        The argument is the bare spec rather than a wrapper carrying identity:
        `ConnectorJobWorkflow` is the parent and already holds the actor and the session — it
        stamps the audit trail and sends the push-back on this run's behalf. A connector
        re-declaring those fields would be a second place for them to be wrong.
        """
        result = await workflow.execute_activity(
            run_xtb_calculation,
            spec,
            start_to_close_timeout=timedelta(seconds=settings.xtb_job_timeout_seconds),
            # The activity heartbeats between species and scan points. Without a heartbeat timeout
            # those heartbeats do nothing for failure detection, and a worker that dies mid-job
            # would be noticed only when the hour-long start-to-close budget expired — which on
            # minute-scale work is the difference between a retry and a wasted afternoon.
            heartbeat_timeout=timedelta(seconds=settings.xtb_job_heartbeat_timeout_seconds),
            # A malformed request (unbalanced equation, unknown solvent, bad atom index) is a
            # `ValueError` no retry can fix — the same non-retryable class the QM job uses, so a
            # bad input fails fast instead of burning the budget.
            retry_policy=BAD_DATA_RETRY,
        )
        # `exclude_none` keeps the envelope to the one result shape that actually ran:
        # `XtbJobResult` carries five optional fields and populates exactly one, so without it
        # every reaction result would ship four explicit nulls for the model to read past.
        return ConnectorJobResult(
            summary=result.summary,
            data=result.model_dump(mode="json", exclude_none=True),
        )
