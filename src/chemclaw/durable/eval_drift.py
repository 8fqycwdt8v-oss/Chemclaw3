"""Durable eval-drift workflow on the background queue.

Re-runs the committed eval case-set on a cadence, compares each metric to the Git-committed
baseline, and pushes any metric outside the relative noise band to a system channel.

The committed case-set is deterministic, so this is a deployment-consistency tripwire: it fires
only when baseline, code and cases were committed inconsistently. Runtime quality drift needs a
live-graph eval, which is deferred (docs/planning/BACKLOG.md). Scoring is pure and lives in
`chemclaw.evals.baseline`; this file is only the Temporal shell.
"""

import asyncio
import logging
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.evals.baseline import (
        DriftAlert,
        aggregate_metrics,
        detect_drift,
        load_baseline,
    )
    from chemclaw.evals.harness import load_eval_cases, run_eval

from chemclaw.durable.notify import notify_session
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# The system push-back channel a drift alert lands on (a `session_events` "session" an operator
# surface tails). A fixed internal id, not a tunable.
DRIFT_ALERT_CHANNEL = "system-eval-drift"

logger = logging.getLogger(__name__)


@durable_activity("background")
@activity.defn
async def check_eval_drift() -> list[DriftAlert]:
    """Score the committed case-set and return the metrics that drifted from the baseline.

    All the I/O and the pure comparison run in this one activity so the workflow stays
    deterministic. Scoring runs in a worker thread because some case metrics drive a retriever via
    `asyncio.run`, which cannot nest in this loop. Each alert is also logged at WARNING, since
    nothing consumes the system channel yet.
    """
    report = await asyncio.to_thread(
        run_eval, load_eval_cases(settings.eval_case_dir), "drift-check"
    )
    current = aggregate_metrics(report)
    baseline = load_baseline(settings.eval_baseline_path)
    alerts = detect_drift(baseline, current, settings.eval_drift_epsilon)
    for alert in alerts:
        if alert.vanished:
            logger.warning(
                "eval drift: metric %r disappeared from the run (baseline %.4f) — its case was "
                "removed or errored; this is not a score of 0.0",
                alert.metric,
                alert.baseline_value,
            )
        else:
            logger.warning(
                "eval drift: metric %r scored %.4f vs baseline %.4f (delta %+.4f)",
                alert.metric,
                alert.current_value,
                alert.baseline_value,
                alert.delta,
            )
    return alerts


@durable_workflow("background")
# Fails rather than parks: the alerts are computed before a park and replayed from history, so a
# resumed run would deliver a stale verdict as current. A failed run also keeps delivery failure
# visible.
@workflow.defn(failure_exception_types=[Exception])
class EvalDriftWorkflow:
    """Run a drift check and deliver one alert per drifted metric to the system channel."""

    @workflow.run
    async def run(self) -> int:
        """Check for drift; deliver each alert (must-deliver). Returns the number of alerts raised.

        Delivery is not best-effort: a failed `session_events` write fails the workflow rather than
        silently dropping a regression alert.
        """
        alerts = await workflow.execute_activity(
            check_eval_drift,
            start_to_close_timeout=timedelta(seconds=settings.eval_drift_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        for alert in alerts:
            await notify_session(DRIFT_ALERT_CHANNEL, "eval_drift", alert.model_dump())
        return len(alerts)
