"""Deliver standing-query digests (gap IDEA-1).

`agent/subscriptions.py` stores what each chemist asked to be told about; this is the job that
tells them. It re-runs each saved query on a cadence and pushes only what has appeared since that
subscriber was last told, through the *existing* session push-back channel (F3-T3) — no new
delivery mechanism, no email integration, no second notification system.

**Where it lands, and who reads it.** Each subscriber has one mailbox in `session_events`, keyed by
the synthetic session id `digest_channel(owner)`, and the front door's `GET /digests` claims it —
scoped to `DIGEST_KIND` so the claim cannot destroy another consumer's rows, and addressed by the
*authenticated* caller's `oid` rather than by anything in the request. That route is what makes the
acknowledgement below true, and it did not exist for the whole first life of this job: the only
consumer in the tree was `GET /sessions/{id}/events`, which 404s a synthetic id no `session_owners`
row backs and claims only the two job-outcome kinds anyway. Measured against a real row: the claim
returned `[]` and left it `consumed_at IS NULL`, which `durable/retention.py` then declines to prune
forever — while the watermark below advanced past it
(`D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`).

**Why the watermark advances on the mailbox write, not on the chemist reading it.** A crash between
"found matches" and "handed to the mailbox" must cause a re-report, not a silent skip: a duplicate
digest line is a nuisance, a missed one defeats the entire feature. The mailbox row is the durable
handover — retention prunes a `session_events` row only once it is *consumed*, so an unread digest
is never aged out — so once the insert commits, re-collecting the same notes would append a second
row for them every cadence instead of protecting anything. What must not advance is the watermark
after a *swallowed* delivery, which is why the acknowledgement is conditional on
`notify_session_best_effort` returning True. That ordering is the only genuinely tricky thing here.

**Why per-subscription isolation.** One subscriber's broken query (or a full mailbox) must not stop
every other chemist's digest, so each is delivered independently and a failure is logged and
skipped — the same reject-and-continue discipline the ELN sync uses.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.subscriptions import Subscription, all_subscriptions, mark_reported
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import degraded
    from chemclaw.deliver.message import Message
    from chemclaw.deliver.registry import deliver, delivery_enabled
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.kg.graph import load_notes
    from chemclaw.kg.note import Note
    from chemclaw.kg.search import query_terms, term_coverage

from chemclaw.durable.notify import notify_session_best_effort
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)

#: The `session_events` kind a digest lands under. Named once because the writer below and the
#: reader (`api/routes/streams.read_digests`) must agree exactly: the claim is destructive and
#: kind-scoped, so a reader claiming a kind the writer does not use consumes nothing and a reader
#: claiming more than this would destroy rows meant for another consumer.
DIGEST_KIND = "digest"


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

    Matching mirrors `find_notes` — the same haystack and tokenizer, from `chemclaw.kg.search` —
    so a chemist's watch behaves the same way as the search they would otherwise re-run by hand.
    It says so because it now calls the same code, not because two implementations were compared
    once. Freshness is judged on the note's
    own `valid_from` (populated from the experiment date, gap KNW-1) — the honest "when did this
    become knowledge" signal, rather than a file mtime that a git sync would reset on every pull.

    Notes are read through `settings.knowledge_path`, like every other reader. This was
    `Path(settings.knowledge_dir)` raw, which resolves against the process CWD (`/app` in the image)
    rather than the note repo — so in any deployment that points `note_repo_dir` at a dedicated
    clone, the digest scanned a different tree from the one the graph is published to and reported
    nothing, silently: an empty scan is not an error, it is just no new matches.

    **The read and the match are one `to_thread` hop**, because this is a coroutine on the
    `background-jobs` worker's single loop, which `worker_max_concurrent_activities` shares with
    seven other activities — including `beating()`'s heartbeat timers for a CREST search that costs
    hours if one is missed. `load_notes` is a recursive `rglob` + `stat` + frontmatter parse and the
    match pass is O(subscriptions x notes) of pure Python; run inline they held the loop for the
    whole of it (measured on a 2,000-note corpus: 1,223.8 ms of loop stall against 27.0 ms
    threaded, for identical work). One hop rather than two also keeps `kg.graph._corpus_lock` — a
    blocking `threading.RLock` — off the loop, which is the condition that lock's own design
    assumes of every caller.
    """
    subscriptions = await all_subscriptions()
    return await asyncio.to_thread(_match_corpus, subscriptions)


def _match_corpus(subscriptions: Sequence[Subscription]) -> list[DigestItem]:
    """Read the corpus and find each subscription's new matches — the whole blocking half.

    Split out purely so `collect_digests` has one thing to offload; the body is unchanged.
    """
    notes = load_notes(settings.knowledge_path)
    digests: list[DigestItem] = []
    for subscription in subscriptions:
        # Tokenized once per subscription, not once per note: the query does not vary across the
        # corpus, and this loop is subscriptions × notes. Measured over 50 subscriptions and 2,000
        # notes, hoisting it took the match pass from 352 ms to 225 ms — on an hourly activity that
        # holds a worker for the whole of it.
        terms = query_terms(subscription.query)
        matches = [
            note.id
            for note in notes
            if _matches(note, subscription, terms) and _is_new(note, subscription)
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


def _matches(note: Note, subscription: Subscription, terms: Sequence[str]) -> bool:
    """Whether a note satisfies a subscription's query and optional type filter.

    Matching really does mirror `find_notes` now: the same haystack and the same tokenizer
    (`chemclaw.kg.search`), every term required. This docstring claimed the mirror while the code
    built a third haystack of its own — narrower (no type, no structure) and whole-phrase, so a
    chemist who subscribed to "biaryl coupling" was told about nothing unless a note contained
    that exact run of text, while the same words typed into `find_notes` found three notes.

    `terms` is `query_terms(subscription.query)`, passed in rather than derived here because the
    caller loops this over the whole corpus and the answer is the same for every note.
    """
    if subscription.note_type and note.type != subscription.note_type:
        return False
    return bool(terms) and term_coverage(note, terms) == len(terms)


def _is_new(note: Note, subscription: Subscription) -> bool:
    """Whether a note became knowledge after this subscriber was last told.

    `>=` rather than `>` on the date, because a note's `valid_from` is a *date* and the digest
    runs hourly: `>` would silently drop a note that appeared later on the same day the digest
    ran, which is the common case and the failure this feature exists to prevent.

    `>=` alone re-qualified every same-day note on every run — up to 24 deliveries a day, against
    `agent/subscriptions.py`'s promise that "asking twice does not double-notify" (DARK-7). The
    subscription therefore also remembers which ids it sent *at that date*, which separates
    "dated today and already sent" from "dated today and new" without having to choose between
    the two failures.
    """
    valid_from = note.valid_from
    if valid_from is None or subscription.last_seen_at is None:
        # No date on either side: report it once. Being told about something already seen is a
        # nuisance; never being told is the failure this feature exists to prevent.
        return True
    if valid_from > subscription.last_seen_at.date():
        return True
    if valid_from < subscription.last_seen_at.date():
        return False
    return note.id not in subscription.last_seen_note_ids


@durable_activity("background")
@activity.defn
async def acknowledge_digest(subscription_id: int, note_ids: list[str]) -> None:
    """Advance a subscription's watermark and record what it delivered, once delivery succeeded."""
    await mark_reported(subscription_id, note_ids)


class DeliveryInput(BaseModel):
    """The typed argument for `deliver_digest_activity`."""

    owner: str
    query: str
    note_ids: list[str] = Field(default_factory=list)


@durable_activity("background")
@activity.defn
async def deliver_digest_activity(payload: DeliveryInput) -> list[str]:
    """Send one subscriber's digest on every enabled outbound channel, and say which took it.

    An activity because it is I/O, and **the enablement check belongs here rather than in the
    workflow**: `delivery_enabled()` reads `settings`, and a workflow that branched on it decided
    whether to emit a command at all — so enabling a channel and restarting a worker made an
    in-flight digest replay a command its history does not contain.

    It never raises. The registry's `deliver` already swallows a single channel's failure so one
    broken webhook is not everyone's outage; a misconfigured seam — a channel named in
    `CHEMCLAW_DELIVERY_CHANNELS` with no folder — raises `DeliveryChannelError`, which is in
    `_BAD_DATA_TYPES` and would therefore fail this activity **non-retryably**. That mattered
    because the caller is ordered before `acknowledge_digest`: one misspelled channel name meant the
    watermark never advanced, so subscriber #1 received the identical digest every night and
    everyone after them received nothing, indefinitely. The failure is caught and reported instead,
    and the caller now runs after the acknowledgement regardless.

    Returns:
        The channels that took the message. Empty means either that delivery is off or that every
        channel refused — which the log line distinguishes and a caller cannot.
    """
    if not delivery_enabled():
        return []
    try:
        # **Inside the `try`, and this is not tidiness.** `Message.recipient` is `min_length=1`, so
        # an empty `Subscription.owner` raises `ValidationError` — which is in `_BAD_DATA_TYPES` and
        # would therefore fail this activity *non-retryably*, aborting the run and every subscriber
        # after it. The docstring said "it never raises" while two lines sat outside the guard that
        # makes that true.
        message = Message(
            recipient=payload.owner,
            subject=f"New for your standing query: {payload.query}",
            body="\n".join(f"- {note_id}" for note_id in payload.note_ids),
            kind="digest",
        )
        taken = await deliver(message)
    except Exception as exc:
        # **Never silently.** Before this, a total delivery failure moved no counter and wrote no
        # log line anywhere in `chemclaw.deliver.registry`, and the workflow discarded the return
        # value — so
        # "every digest was dropped" and "every digest was delivered" were the same observation.
        degraded(
            logger,
            "digest_delivery",
            "digest delivery failed for %s: %s",
            payload.owner,
            exc,
        )
        return []
    if not taken:
        degraded(
            logger,
            "digest_delivery",
            "no channel took the digest for %s; %d channel(s) are enabled",
            payload.owner,
            len(settings.delivery_channel_list),
        )
    return taken


@durable_workflow("background")
@workflow.defn
class DigestWorkflow:
    """Deliver each subscriber's standing-query digest, then advance their watermark."""

    @workflow.run
    async def run(self) -> int:
        """Deliver every pending digest; return how many were sent."""
        timeout = timedelta(seconds=settings.digest_timeout_seconds)
        digests = await workflow.execute_activity(
            collect_digests,
            start_to_close_timeout=timeout,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        delivered = 0
        for item in digests:
            # Best-effort per subscriber: one broken mailbox must not stop everyone else's digest.
            sent = await notify_session_best_effort(
                digest_channel(item.owner),
                DIGEST_KIND,
                {"query": item.query, "note_ids": item.note_ids},
            )
            # Only after delivery — see the module docstring on why this ordering matters. The
            # acknowledgement used to run unconditionally, which made a swallowed delivery failure
            # indistinguishable from a successful send and advanced the watermark past matches the
            # subscriber never received. Those notes can never re-qualify, so the guarantee the
            # ordering exists to provide ("a crash between 'found matches' and 'delivered' must
            # cause a re-report, not a silent skip") held for a crash and not for the failure mode
            # that actually happens.
            if not sent:
                continue
            await workflow.execute_activity(
                acknowledge_digest,
                args=[item.subscription_id, item.note_ids],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            # **Also out of the building, when a deployment has said where — and strictly after
            # the acknowledgement.** The mailbox above is the durable handover and is what the
            # watermark turns on: a chemist who opens the app must see the digest whether or not a
            # channel took it, and a channel outage must not re-report matches the mailbox already
            # holds. That was the stated intent and the code did the opposite — this ran *before*
            # `acknowledge_digest`, and `deliver_digest_activity` could fail non-retryably on a
            # misspelled channel name, so the watermark never advanced: subscriber #1 got the same
            # digest every night and everyone after them got nothing. The activity now reports its
            # own failures and never raises, and it runs last so that even a raise could not
            # unacknowledge a delivered digest. Its result is deliberately not part of the
            # acknowledging condition: outbound delivery is a courtesy on top of a delivered digest,
            # not a second delivery the watermark waits on.
            await workflow.execute_activity(
                deliver_digest_activity,
                DeliveryInput(owner=item.owner, query=item.query, note_ids=list(item.note_ids)),
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            delivered += 1
        return delivered


def digest_channel(owner: str) -> str:
    """The per-user mailbox a digest lands in, addressed by the owner's Entra `oid`.

    Public and imported by the reader (`api/routes/streams.read_digests`) rather than restated
    there: this string is the whole addressing scheme, so a second spelling of it would be a
    mailbox the job writes to and nobody opens — which is exactly the state
    `D-2026-08-27-a-digest-nobody-can-read-is-not-delivered` found.

    It is a *synthetic* session id: no `session_owners` row backs it, so it cannot be authorized by
    session ownership. The reader therefore never takes it from a caller — it derives it from the
    authenticated principal, which is why one chemist cannot name another's mailbox.
    """
    return f"digest-{owner}"
