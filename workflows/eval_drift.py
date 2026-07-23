"""Durable eval-drift workflow (plan F10-F2) on the background queue.

Re-runs the committed eval case-set on a cadence, aggregates each metric, and compares it to the
Git-committed baseline; any metric that moved beyond the relative noise band is pushed to a system
channel so an operator sees a regression instead of it going unnoticed.

Scope note (honesty about what this catches): the committed case-set is deterministic, so over it
this is a *deployment-consistency tripwire* — it fires only if the deployed baseline, code, and
cases were committed inconsistently (the same condition the CI guard catches at merge). Real
runtime *quality* drift needs a non-deterministic eval (retrieval P/R/F1 over the deployment's own
live graph), which is deployment-local and deferred — see DEFERRED.md. The scoring work — run +
aggregate + compare — is pure and lives in `evals.baseline` (fully unit-tested); this file is only
the Temporal shell: one activity does the file I/O, the workflow delivers each alert via the
`notify` seam. Durability of the *schedule* lives in Temporal (D-035), not host cron.
"""

import asyncio
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from evals.baseline import (
        DriftAlert,
        aggregate_metrics,
        detect_drift,
        load_baseline,
    )
    from evals.harness import load_eval_cases, run_eval

from workflows.notify import notify_session
from workflows.publish import BAD_DATA_RETRY

# The well-known system push-back channel a drift alert lands on (a `session_events` "session" an
# operator surface tails). A fixed internal id, not a tunable threshold — analogous to the schedule
# ids in `scripts/schedules.py` — so it is a constant here, not a config knob.
DRIFT_ALERT_CHANNEL = "system-eval-drift"


@activity.defn
async def check_eval_drift() -> list[DriftAlert]:
    """Score the committed case-set and return the metrics that drifted from the baseline.

    All the I/O (reading cases + the baseline file) and the pure comparison run in this one
    activity, so the workflow stays deterministic and this is the single side-effecting step.
    Scoring runs in a worker thread: some case metrics (the KM-13 retrieval gold set) drive a live
    retriever via `asyncio.run`, which cannot nest inside this activity's own event loop.
    """
    report = await asyncio.to_thread(
        run_eval, load_eval_cases(settings.eval_case_dir), "drift-check"
    )
    current = aggregate_metrics(report)
    baseline = load_baseline(settings.eval_baseline_path)
    return detect_drift(baseline, current, settings.eval_drift_epsilon)


@workflow.defn
class EvalDriftWorkflow:
    """Run a drift check and deliver one alert per drifted metric to the system channel."""

    @workflow.run
    async def run(self) -> int:
        """Check for drift; deliver each alert (must-deliver). Returns the number of alerts raised.

        The alert is this workflow's only operator-facing output, so delivery is *not* best-effort:
        a failed `session_events` write fails the workflow (visible as a failed run) rather than
        silently dropping a regression alert.
        """
        alerts = await workflow.execute_activity(
            check_eval_drift,
            start_to_close_timeout=timedelta(seconds=settings.eval_drift_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
        for alert in alerts:
            await notify_session(DRIFT_ALERT_CHANNEL, "eval_drift", alert.model_dump())
        return len(alerts)
