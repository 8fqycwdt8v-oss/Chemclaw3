"""The `calc` connector's durable workflow: one expensive calculation, run to completion.

A reaction or solvent screen with optimizations and Hessians per species takes minutes, longer
than a conversation can hold open. The starting tool waits a bounded moment
(`JobSpec.inline_wait_seconds`) and reports the result if it arrives, otherwise a job id, so the
split is decided by what happened rather than by a cost model.

Deterministic orchestration only; everything non-deterministic lives in
`chemclaw.connectors.calc.activities`. Runs on this bundle's own queue, started by
`ConnectorJobWorkflow` under the type name its manifest declares.
"""

from datetime import timedelta

from temporalio import workflow

# Activities, models, and config are ordinary modules that must bypass the workflow sandbox's
# re-import isolation (the standard Temporal pattern).
with workflow.unsafe.imports_passed_through():
    from chemclaw.connectors.calc.activities import run_xtb_calculation
    from chemclaw.connectors.calc.results import XtbJobResult
    from chemclaw.connectors.calc.specs import XtbJobSpec
    from chemclaw.core.config import settings
    from chemclaw.durable.connector_job import ConnectorJobResult
    from chemclaw.science.calc.geometry import without_geometry

from chemclaw.connectors.queues import bundle_queue
from chemclaw.durable.publish import calculation_retry, connector_queue_wait_timeout
from chemclaw.durable.registry import durable_workflow


def job_envelope(result: XtbJobResult) -> ConnectorJobResult:
    """This bundle's activity result, as the envelope core carries, publishes and pushes back.

    A pure module-level function, so a replay is byte-identical and a test can call exactly what the
    workflow calls. `data` is the domain result (the member, not the `XtbJobResult` wrapper),
    because
    `publish` may not import `connectors` to unwrap it. `calc_refs` rides on the envelope's own
    field.
    `without_geometry` replaces each geometry with its address, keeping coordinates out of the turn.
    """
    outcome = result.outcome()
    return ConnectorJobResult(
        summary=result.summary,
        calc_refs=result.calc_refs,
        # The result model's name is the only thing that lets `chemclaw.publish` route a composite,
        # whose
        # `calc_type` matches no projector prefix.
        payload_kind=type(outcome).__name__,
        # Ship only what the calculation reported, not explicit nulls.
        data=without_geometry(outcome.model_dump(mode="json", exclude_none=True)),
    )


@durable_workflow(bundle_queue("calc"))
# Without this the SDK retries a plain exception in workflow code forever, so the parent
# `ConnectorJobWorkflow` and the chemist wait indefinitely. See `durable/connector_job.py`.
@workflow.defn(failure_exception_types=[Exception])
class CalcJobWorkflow:
    """Run one expensive xTB calculation durably and return it in the connector envelope."""

    @workflow.run
    async def run(self, spec: XtbJobSpec) -> ConnectorJobResult:
        """Execute the calculation; safe to replay and to resume after a worker restart.

        Takes the bare spec: the parent `ConnectorJobWorkflow` holds the actor and session and does
        the
        audit and push-back on this run's behalf.
        """
        result = await workflow.execute_activity(
            run_xtb_calculation,
            # Actor and correlation id come from the run's memo, set by `ConnectorJobWorkflow`. They
            # are
            # arguments, not part of `spec`, because `spec`'s digest is the cache key. The activity
            # stamps them
            # on its calls to the calculation server so durable runs are attributed like inline
            # ones.
            args=[
                spec,
                workflow.memo_value("requested_by", settings.service_actor_id),
                workflow.memo_value("correlation_id", ""),
            ],
            start_to_close_timeout=timedelta(seconds=settings.xtb_job_timeout_seconds),
            # `start_to_close` does not bound the queue wait. A real backlog is backpressure and
            # must pass; this
            # bounds the other case, no worker serving `connector-calc` at all (see
            # `durable/publish.py`).
            schedule_to_start_timeout=connector_queue_wait_timeout(),
            # The activity heartbeats between species and scan points; this timeout is what turns a
            # dead worker
            # into a prompt retry instead of waiting out start-to-close.
            heartbeat_timeout=timedelta(seconds=settings.xtb_job_heartbeat_timeout_seconds),
            # Bad input still fails fast (`BAD_DATA_RETRY`'s type list); the backoff is sized to
            # wait out a
            # saturated backend (`CalcBusyError`), which a default-spaced retry would merely spin
            # against.
            retry_policy=calculation_retry(),
        )
        # Applied here, not in the activity, because the activity's return type is pinned by
        # histories in
        # flight; `job_envelope` is pure, so replay is unaffected.
        return job_envelope(result)
