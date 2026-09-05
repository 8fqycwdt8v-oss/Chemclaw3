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

    **A module-level function rather than three lines inside `run`, for the reason
    `without_geometry` beside it is one.** The property worth asserting is that this is a *pure*
    function of a value
    already in history — a replay must produce byte-identical output — and a test can only assert
    that by calling the same thing the workflow calls. When this was inlined, the test that existed
    to prove the publish path routed built its own envelope by hand and paired the route with the
    inner model, while the workflow paired it with the wrapper: the copy agreed with nobody, and
    all nine of this bundle's jobs published nothing behind a green suite
    (`D-2026-08-25-a-cache-is-not-a-record`).

    Three facts about the shape it produces, each of which was once wrong:

    - **`data` is the domain result, not the envelope around it.** `XtbJobResult` is bookkeeping —
      `kind` and `summary` — and both already ride on `ConnectorJobResult` in their own right.
      Sending the wrapper put the science one level down (`data.ensemble.…`) and made
      `payload_kind` read `XtbJobResult` for every job, which matches no projector. Publishing the
      member is what the removed DFT bundle always did, and it is the only fix available here:
      `publish` may not import `connectors` (`tests/test_layering.py` allows that edge one way
      only), so the unwrapping cannot live on the far side.
    - **`calc_refs` rides on the envelope's own field**, not inside `data`: it is a cross-cutting
      provenance fact every connector job could carry rather than this bundle's domain result, and
      `propose_knowledge_note` takes it as one list.
    - **`without_geometry` replaces each geometry with the address the next calculation accepts.**
      Measured on a 40-atom molecule, a conformer search's envelope was 29,086 characters — 2,400
      Cartesian coordinates no tool in this system accepts — reaching the turn three times over
      (D-2026-08-21-a-geometry-is-an-address-not-a-payload).
    """
    outcome = result.outcome()
    return ConnectorJobResult(
        summary=result.summary,
        calc_refs=result.calc_refs,
        # The result model's own name, so `chemclaw.publish` can route a composite exactly. A
        # composite has no cache key, so its `calc_type` is `<connector>.<job>` and matches no
        # projector prefix -- this is the only thing that identifies the shape.
        payload_kind=type(outcome).__name__,
        # `exclude_none` keeps the payload to what the calculation actually reported, dropping the
        # optional fields this result shape left empty rather than shipping explicit nulls for the
        # model to read past.
        data=without_geometry(outcome.model_dump(mode="json", exclude_none=True)),
    )


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
            # The actor and the correlation id come off the run's **memo**, which
            # `ConnectorJobWorkflow` sets on every connector job for exactly this (D-118) — the
            # same read `connectors/bo/workflows.py` makes for its campaign record. They are
            # arguments rather than part of `spec` because `spec` is the model-authored payload and
            # its digest is the cache key: identity must not be able to change either.
            #
            # What they are *for* is the call this activity makes back out to the calculation
            # server. `science/calc/store.py::cached_compute` opens an MCP session per call and
            # `core.mcp_session.open_session` stamps the ambient identity onto it — ambient that
            # nothing on this path ever set, so the heaviest server in the fleet logged
            # `actor=- session=-` for every durable run while the same tool called inline from a
            # chat turn was fully attributed.
            args=[
                spec,
                workflow.memo_value("requested_by", settings.service_actor_id),
                workflow.memo_value("correlation_id", ""),
            ],
            start_to_close_timeout=timedelta(seconds=settings.xtb_job_timeout_seconds),
            # **`start_to_close` starts when a worker picks the task up, so it bounds none of the
            # wait.** This bundle's queue is one pod of eight slots and each `run_xtb_calculation`
            # holds one for the whole composite, so at target load the backlog is real: measured on
            # the broker, p50 schedule->start ~1.04 h and p95 ~1.98 h at a 300 s activity. That is
            # backpressure and must pass. What must not pass unnoticed is the other shape — nothing
            # serving `connector-calc` at all — which was bounded only by the parent's five-hour
            # execution timeout, i.e. by a failure that names neither the queue nor the reason and
            # is delivered to no workflow code. `durable/publish.py` states the bound once.
            schedule_to_start_timeout=connector_queue_wait_timeout(),
            # The activity heartbeats between species and scan points. Without a heartbeat timeout
            # those heartbeats do nothing for failure detection, and a worker that dies mid-job
            # would be noticed only when the hour-long start-to-close budget expired — which on
            # minute-scale work is the difference between a retry and a wasted afternoon.
            heartbeat_timeout=timedelta(seconds=settings.xtb_job_heartbeat_timeout_seconds),
            # A malformed request (unbalanced equation, unknown solvent, bad atom index) is a
            # `ValueError` no retry can fix, so a bad input still fails fast instead of burning the
            # budget: `calculation_retry` carries `BAD_DATA_RETRY`'s type list unchanged. What it
            # adds is a backoff sized to the one failure that *is* worth waiting out — the shared
            # calculation backend refusing because every slot is taken (`CalcBusyError`). At
            # Temporal's default spacing five attempts fit inside fifteen seconds, which is a spin
            # against a hold that is a whole calculation long.
            retry_policy=calculation_retry(),
        )
        # Applied here rather than in the activity because the activity's return type is
        # pinned by workflow histories in flight, and because `job_envelope` is a pure
        # function of a value already in history: a replay produces byte-identical output
        # from the same recorded result. It is a module-level function so that the test
        # proving the publish path routes can call the same thing this line calls.
        return job_envelope(result)
