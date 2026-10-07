"""Deliver standing-query digests.

`agent/subscriptions.py` stores what each chemist asked to be told about; this job re-runs each
saved query on a cadence and pushes only what appeared since that subscriber was last told.

Each subscriber has one mailbox in `session_events`, keyed by `digest_channel(owner)` and claimed
by `GET /digests` for the authenticated caller only.

The watermark advances on the mailbox write, not on the chemist reading it: a crash before the
handover must cause a re-report, not a skip, and an unread mailbox row is never pruned. It must
not advance after a swallowed delivery, so acknowledgement is conditional on
`notify_session_best_effort` returning True. Each subscription is delivered independently, so
one broken query does not stop the others.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.subscriptions import Subscription, all_subscriptions, mark_reported
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.kg.conflicts import conflict_index
    from chemclaw.kg.graph import load_notes, note_arrivals
    from chemclaw.kg.note import Note
    from chemclaw.kg.search import query_terms, term_coverage

from chemclaw.durable.deliver_message import (
    OutboundMessage,
    deliver_best_effort,
    deliver_message_activity,
)
from chemclaw.durable.notify import notify_session_best_effort
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)

#: The `session_events` kind a digest lands under. Shared with the reader
#: (`api/routes/streams.read_digests`), whose claim is destructive and kind-scoped.
DIGEST_KIND = "digest"


class DigestItem(BaseModel):
    """One subscriber's new matches since they were last told, and which of them are disputed.

    **A digest that lists a contradiction as an ordinary find is the failure this field exists
    for.** `kg/conflicts.py` has always known which notes disagree, and
    `retrieval.retrievers._conflict_index` flags every chunk of a disputed note at *retrieval*
    time — so a chemist who happens to ask is told, and a chemist watching the subject is not. The
    corpus starting to disagree with itself on somebody's standing query is the one thing in a
    digest that changes what they should do next.

    `disputed` is a subset of `note_ids`, carried as its own list rather than as a flag per id,
    because that is the shape the body renders and the shape a client can count. It defaults to
    empty for the reason every added field here must: this model is an activity's *return*, and a
    run opened on the previous release replays a recorded result that has no such key.

    **`headlines` is what makes a digest readable, and its absence is why one was not.** Every
    surface downstream — the text body, the mailbox payload, `GET /digests`, the card on the UI's
    `/review` — named each match by its **note id**, so the one proactive thing this system does
    told a chemist `playbook-aee3d30407cc` and left them to go and look it up. Nothing else could
    be sent: `Note` has no title field, and the id is the only handle the watermark works in. The
    job has already parsed every note by the time it builds this, so `Note.headline()` costs one
    string per match and says what the match *is*.

    A mapping keyed by id rather than a list parallel to `note_ids`, because the two must not be
    able to come apart: a parallel list that is reordered or short by one relabels somebody's
    evidence. A missing key is a note with no body, which a reader shows as the id.
    """

    subscription_id: int
    owner: str
    query: str
    note_ids: list[str]
    disputed: list[str] = Field(default_factory=list)
    headlines: dict[str, str] = Field(default_factory=dict)


@durable_activity("background")
@activity.defn
async def collect_digests() -> list[DigestItem]:
    """Find, per subscription, the notes matching it that appeared since its watermark.

    Matching uses `find_notes`' haystack and tokenizer (`chemclaw.kg.search`), so a watch behaves
    like the search it replaces. Freshness is the note's `valid_from`; a note without one is judged
    on when its file was committed (`kg.graph.note_arrivals`). Notes are read through
    `settings.knowledge_path` like every other reader.

    The read and match run in one `to_thread` hop: both are blocking and O(subscriptions x notes),
    and the worker's loop also carries heartbeat timers for long jobs. One hop also keeps
    `kg.graph._corpus_lock` (a blocking `RLock`) off the loop.
    """
    subscriptions = await all_subscriptions()
    return await asyncio.to_thread(_match_corpus, subscriptions)


def _match_corpus(subscriptions: Sequence[Subscription]) -> list[DigestItem]:
    """Read the corpus and find each subscription's new matches — the whole blocking half."""
    if not subscriptions:
        return []
    notes = load_notes(settings.knowledge_path)
    # One conflict scan for every subscription; usually cold in this process, hence the early
    # return above when there are no subscriptions. Scoped `as_of` today so a superseded note is not
    # reported as disagreeing with its own replacement.
    disputes = conflict_index(settings.knowledge_path, date.today())
    # When each note reached the corpus, for the notes that carry no `valid_from` — see `_is_new`.
    # One `git log` per run, incremental after the first in this process.
    arrivals = note_arrivals(settings.knowledge_path)
    digests: list[DigestItem] = []
    for subscription in subscriptions:
        # Tokenized once per subscription, not per note: this loop is subscriptions x notes.
        terms = query_terms(subscription.query)
        matches = [
            note.id
            for note in notes
            if _matches(note, subscription, terms)
            and _is_new(note, subscription, arrivals.get(note.id))
        ]
        if matches:
            digests.append(
                DigestItem(
                    subscription_id=subscription.id,
                    owner=subscription.owner,
                    query=subscription.query,
                    note_ids=sorted(matches),
                    disputed=sorted(one for one in matches if one in disputes),
                    headlines={
                        note.id: headline
                        for note in notes
                        if note.id in matches and (headline := note.headline())
                    },
                )
            )
    return digests


def _digest_body(
    note_ids: Sequence[str],
    disputed: Sequence[str],
    headlines: Mapping[str, str] | None = None,
) -> str:
    """The lines a subscriber reads: every new note, and which of them the corpus disputes.

    Shared by every renderer so a digest says the same thing everywhere. A disputed note is marked
    in place, with a trailing count. Each line leads with the note's headline and keeps the id in
    brackets (what `GET /notes/{id}` takes). `headlines` is optional for the replay shim, which has
    only ids.
    """
    marked = set(disputed)
    named = headlines or {}
    lines = [
        f"- {named.get(note_id) or note_id}"
        f"{f' [{note_id}]' if named.get(note_id) else ''}"
        f"{' (disputed)' if note_id in marked else ''}"
        for note_id in note_ids
    ]
    if marked:
        lines.append(
            f"\n{len(marked)} of {len(note_ids)} disagree with something already in the graph. "
            "Read those beside what they contradict before acting on them."
        )
    return "\n".join(lines)


def _matches(note: Note, subscription: Subscription, terms: Sequence[str]) -> bool:
    """Whether a note satisfies a subscription's query and optional type filter.

    Same haystack and tokenizer as `find_notes`, every term required. `terms` is passed in because
    it is the same for every note.
    """
    if subscription.note_type and note.type != subscription.note_type:
        return False
    return bool(terms) and term_coverage(note, terms) == len(terms)


def _is_new(note: Note, subscription: Subscription, arrived: date | None = None) -> bool:
    """Whether a note became knowledge after this subscriber was last told.

    `>=` on the date, because `valid_from` is a date and the digest runs hourly: `>` would drop a
    note dated the same day as the last run. To avoid re-sending same-day notes, the subscription
    also remembers which ids it sent at that date.

    A note with no `valid_from` is judged on `arrived`, the date its file was committed to the
    corpus (`kg.graph.note_arrivals`); it stands in for nothing else. With neither date (a corpus
    that is not a git work tree), the note is not new.
    """
    when = note.valid_from or arrived
    if subscription.last_seen_at is None:
        # Nobody has been told anything yet, so everything matching is news.
        return True
    if when is None:
        return False
    # In UTC, the zone `note_arrivals` dates in; a naive watermark is read as already UTC.
    seen = subscription.last_seen_at
    seen_on = (seen.astimezone(UTC) if seen.tzinfo else seen).date()
    if when > seen_on:
        return True
    if when < seen_on:
        return False
    return note.id not in subscription.last_seen_note_ids


@durable_activity("background")
@activity.defn
async def acknowledge_digest(subscription_id: int, note_ids: list[str]) -> None:
    """Advance a subscription's watermark and record what it delivered, once delivery succeeded."""
    await mark_reported(subscription_id, note_ids)


class DeliveryInput(BaseModel):
    """The typed argument for `deliver_digest_activity` — kept only for replay.

    See that function. Nothing constructs one on the current path.
    """

    owner: str
    query: str
    note_ids: list[str] = Field(default_factory=list)


@durable_activity("background")
@activity.defn
async def deliver_digest_activity(payload: DeliveryInput) -> list[str]:
    """Deprecated: here only so a run opened on the previous release can replay.

    Such runs have `ActivityTaskScheduled(deliver_digest_activity)` in history; emitting another
    activity type there would park the workflow task, and under SKIP one wedged run silently skips
    every later digest. Scheduled only on the `workflow.patched` off branch, and delegates to
    `deliver_message_activity`. Removable once no pre-patch run can still replay.
    """
    return await deliver_message_activity(
        OutboundMessage(
            recipient=payload.owner,
            subject=f"New for your standing query: {payload.query}",
            # A replayed run's recorded `DigestItem` predates `disputed`, so empty is the truth
            # here.
            body=_digest_body(payload.note_ids, ()),
            kind="digest",
        )
    )


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
                {
                    "query": item.query,
                    "note_ids": item.note_ids,
                    "disputed": item.disputed,
                    "headlines": item.headlines,
                },
            )
            # Only after a successful mailbox delivery — see the module docstring. Acknowledging a
            # swallowed failure would advance the watermark past notes the subscriber never
            # received.
            if not sent:
                continue
            await workflow.execute_activity(
                acknowledge_digest,
                args=[item.subscription_id, item.note_ids],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            # Outbound delivery runs strictly after the acknowledgement and does not affect it: the
            # mailbox is the durable handover, and a channel outage must neither block the watermark
            # nor re-report.
            #
            # Behind a patch: runs opened before it have `deliver_digest_activity` at this position,
            # and the off branch emits exactly that.
            if workflow.patched("digest-outbound-delivery-seam"):
                await deliver_best_effort(
                    OutboundMessage(
                        recipient=item.owner,
                        subject=f"New for your standing query: {item.query}",
                        body=_digest_body(item.note_ids, item.disputed, item.headlines),
                        kind="digest",
                    )
                )
            else:
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

    Imported by the reader (`api/routes/streams.read_digests`) so the addressing has one spelling.
    A synthetic session id with no `session_owners` row, so the reader derives it from the
    authenticated principal and never takes it from a caller.
    """
    return f"digest-{owner}"
