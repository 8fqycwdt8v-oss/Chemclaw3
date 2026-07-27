"""Deliver standing-query digests (gap IDEA-1).

`agents/subscriptions.py` stores what each chemist asked to be told about; this is the job that
tells them. It re-runs each saved query on a cadence and pushes only what has appeared since that
subscriber was last told, through the *existing* session push-back channel (F3-T3) — no new
delivery mechanism, no email integration, no second notification system.

**Why the watermark advances after delivery, not before.** A crash between "found matches" and
"delivered" must cause a re-report, not a silent skip: a duplicate digest line is a nuisance, a
missed one defeats the entire feature. That ordering is the only genuinely tricky thing here.

**Why per-subscription isolation.** One subscriber's broken query (or a full mailbox) must not stop
every other chemist's digest, so each is delivered independently and a failure is logged and
skipped — the same reject-and-continue discipline the ELN sync uses.
"""

import logging
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pathlib import Path

    from agents.subscriptions import Subscription, all_subscriptions, mark_reported
    from chemclaw.config import settings
    from kg.graph import load_notes
    from workflows.registry import durable_activity, durable_workflow

from workflows.notify import notify_session_best_effort
from workflows.publish import BAD_DATA_RETRY

logger = logging.getLogger(__name__)


class DigestItem(BaseModel):
    """One subscriber's new matches since they were last told."""

    subscription_id: int
    owner: str
    query: str
    note_ids: list[str]


@durable_activity("background")
@activity.defn
async def collect_digests() -> list[DigestItem]:
    """Find, per subscription, the notes matching it that appeared since its watermark.

    Matching mirrors `find_notes` (id / tags / body substring), so a chemist's watch behaves the
    same way as the search they would otherwise re-run by hand. Freshness is judged on the note's
    own `valid_from` (populated from the experiment date, gap KNW-1) — the honest "when did this
    become knowledge" signal, rather than a file mtime that a git sync would reset on every pull.
    """
    notes = load_notes(Path(settings.knowledge_dir))
    digests: list[DigestItem] = []
    for subscription in await all_subscriptions():
        matches = [
            note.id
            for note in notes
            if _matches(note, subscription) and _is_new(note, subscription)
        ]
        if matches:
            digests.append(
                DigestItem(
                    subscription_id=subscription.id,
                    owner=subscription.owner,
                    query=subscription.query,
                    note_ids=sorted(matches),
                )
            )
    return digests


def _matches(note: object, subscription: Subscription) -> bool:
    """Whether a note satisfies a subscription's query and optional type filter."""
    note_type = getattr(note, "type", "")
    if subscription.note_type and note_type != subscription.note_type:
        return False
    needle = subscription.query.lower()
    haystack = " ".join(
        [getattr(note, "id", ""), " ".join(getattr(note, "tags", [])), getattr(note, "body", "")]
    ).lower()
    return needle in haystack


def _is_new(note: object, subscription: Subscription) -> bool:
    """Whether a note became knowledge after this subscriber was last told."""
    valid_from = getattr(note, "valid_from", None)
    if valid_from is None or subscription.last_seen_at is None:
        # No date on either side: report it once. Being told about something already seen is a
        # nuisance; never being told is the failure this feature exists to prevent.
        return True
    return bool(valid_from >= subscription.last_seen_at.date())


@durable_activity("background")
@activity.defn
async def acknowledge_digest(subscription_id: int) -> None:
    """Advance a subscription's watermark, once its digest has actually been delivered."""
    await mark_reported(subscription_id)


@durable_workflow("background")
@workflow.defn
class DigestWorkflow:
    """Deliver each subscriber's standing-query digest, then advance their watermark."""

    @workflow.run
    async def run(self) -> int:
        """Deliver every pending digest; return how many were sent."""
        timeout = timedelta(seconds=settings.digest_timeout_seconds)
        digests = await workflow.execute_activity(
            collect_digests, start_to_close_timeout=timeout, retry_policy=BAD_DATA_RETRY
        )
        delivered = 0
        for item in digests:
            # Best-effort per subscriber: one broken mailbox must not stop everyone else's digest.
            await notify_session_best_effort(
                _digest_channel(item.owner),
                "digest",
                {"query": item.query, "note_ids": item.note_ids},
            )
            # Only after delivery — see the module docstring on why this ordering matters.
            await workflow.execute_activity(
                acknowledge_digest,
                item.subscription_id,
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            delivered += 1
        return delivered


def _digest_channel(owner: str) -> str:
    """The per-user push-back channel a digest lands on (a surface tails it, like job push-back)."""
    return f"digest-{owner}"
