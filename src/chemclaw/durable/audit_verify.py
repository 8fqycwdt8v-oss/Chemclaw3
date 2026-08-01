"""Scheduled verification of the tamper-evident audit chain (gap SCH-5).

F10-G1 made the GxP audit trail tamper-evident: each row hashes its predecessor, so a modified,
reordered, or interior-deleted record breaks the chain. Verification shipped as `make audit-verify`
— a manual command. A chain that is only checked when somebody remembers to look detects tampering
only when somebody remembers to look, which is not a control; the whole value of the construction is
that a break is discovered *promptly*.

This runs the existing `verify_chain` on a cadence and alerts through the **must-deliver** notify
seam, exactly as the eval-drift job does (D-069): the alert is this workflow's only operator-facing
output, so a dropped delivery would defeat the feature. A silently un-delivered "your audit trail
has been tampered with" is worse than no check at all, because it reads as an all-clear.

No new verification logic: the workflow is the missing *driver*, and `cli/verify_audit_chain.py`
remains the single implementation shared by the CLI and this job (DRY).
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.audit_anchor import take_anchor
    from chemclaw.cli.verify_audit_chain import verify_chain
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.notify import notify_session
from chemclaw.durable.publish import BAD_DATA_RETRY

# The well-known system channel an integrity alert lands on — the same shape as
# `DRIFT_ALERT_CHANNEL`, and a fixed internal id rather than a config knob for the same reason.
AUDIT_ALERT_CHANNEL = "system-audit-integrity"


@durable_activity("background")
@activity.defn
async def check_audit_chain() -> list[str]:
    """Verify the audit hash chain; return one problem string per break (empty means intact)."""
    return await verify_chain()


@durable_activity("background")
@activity.defn
async def anchor_audit_chain() -> str:
    """Record and publish a signed high-water anchor over the trail; return its `taken_at`.

    Only reached after a clean verification, and that ordering is the whole design: anchoring an
    unverified trail signs whatever damage is already in it and makes that the new baseline. Empty
    when no `audit_anchor_secret` is configured, which is the honest "this control is off" rather
    than a failure — the chain still catches everything it caught before.
    """
    anchor = await take_anchor()
    return anchor.taken_at if anchor is not None else ""


@durable_workflow("background")
@workflow.defn
class AuditChainVerifyWorkflow:
    """Verify the audit chain on a cadence and alert on any break (gap SCH-5)."""

    @workflow.run
    async def run(self) -> int:
        """Verify, deliver one alert per break, and return how many breaks were found.

        Delivery is must-deliver, not best-effort: an integrity alert nobody receives is
        indistinguishable from an intact chain, which is the one outcome this job must never
        produce.
        """
        problems = await workflow.execute_activity(
            check_audit_chain,
            start_to_close_timeout=timedelta(seconds=settings.audit_verify_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
        for problem in problems:
            # Logged as well as notified, and deliberately not instead of. Both consumers of the
            # push-back mailbox scope their claim to `kinds=("job_completed",)`, so an
            # `audit_chain_break` row is not merely unread — it is never even eligible to be
            # claimed. Without this line a detected tampering completed successfully and showed a
            # green schedule, which is exactly the "silently un-delivered alert reads as an
            # all-clear" outcome this workflow's docstring says it must never produce.
            # `durable/eval_drift.py` reached the same conclusion for the same reason; this is that
            # mitigation, which was not carried across with the notify pattern.
            workflow.logger.error("audit chain integrity failure: %s", problem)
            await notify_session(
                AUDIT_ALERT_CHANNEL,
                "audit_chain_break",
                {"detail": problem},
            )
        if not problems:
            # A fresh high-water mark, and only on a clean run. The anchor is what makes a *later*
            # truncation visible — including the one a point-in-time restore performs — so the
            # cadence of this job is also the resolution of that detection: a trail can only be
            # shown to have lost rows since the last anchor, never since the last event.
            await workflow.execute_activity(
                anchor_audit_chain,
                start_to_close_timeout=timedelta(seconds=settings.audit_verify_timeout_seconds),
                retry_policy=BAD_DATA_RETRY,
            )
        return len(problems)
