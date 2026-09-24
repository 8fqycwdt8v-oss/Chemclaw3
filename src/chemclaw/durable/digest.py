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
row backs and claims a named set of kinds that has never included this one. Measured against a
real row: the claim returned `[]` and left it `consumed_at IS NULL`, which
`durable/retention.py` then declines to prune forever — while the watermark below advanced past it
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
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

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

#: The `session_events` kind a digest lands under. Named once because the writer below and the
#: reader (`api/routes/streams.read_digests`) must agree exactly: the claim is destructive and
#: kind-scoped, so a reader claiming a kind the writer does not use consumes nothing and a reader
#: claiming more than this would destroy rows meant for another consumer.
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

    Matching mirrors `find_notes` — the same haystack and tokenizer, from `chemclaw.kg.search` —
    so a chemist's watch behaves the same way as the search they would otherwise re-run by hand.
    It says so because it now calls the same code, not because two implementations were compared
    once. Freshness is judged on the note's
    own `valid_from` (populated from the experiment date, gap KNW-1) — the honest "when did this
    become knowledge" signal, rather than a file mtime that a git sync would reset on every pull.
    A note with no `valid_from` is judged on when its file was committed to the corpus instead
    (`kg.graph.note_arrivals`), which every clone shares for the same reason an mtime is not.

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
    if not subscriptions:
        return []
    notes = load_notes(settings.knowledge_path)
    # One scan for every subscription. **Not warm here, which is what this comment claimed.** It
    # said "cached behind the corpus fingerprint that retrieval already warms", and
    # `kg.conflicts._INDEX_CACHE` is a *process-local* module global: the retrieval callers run in
    # the API and agent processes, this runs in `background_worker`, so nothing they do warms it —
    # and the fingerprint invalidates on exactly the corpus change that makes a digest worth
    # sending. So this is a cold scan on most runs, at `conflict_index`'s own measured 1,525 ms on
    # a 2,000-note corpus (68 ms on the 39-note shipped one). It is affordable on an hourly
    # activity that is already offloaded to a thread; it is not free, and the early return above is
    # there because a deployment with no subscriptions was paying for it in full.
    #
    # Scoped `as_of` today, matching the retrieval-time caller: a superseded note is out of the
    # current-evidence sweep, and reporting it as disagreeing with its own replacement is noise
    # rather than news.
    disputes = conflict_index(settings.knowledge_path, date.today())
    # When each note reached the corpus, for the notes that carry no `valid_from` — see `_is_new`.
    # One `git log` per run, incremental after the first in this process.
    arrivals = note_arrivals(settings.knowledge_path)
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

    One function because three call sites render this — the session mailbox's sibling, the current
    delivery, and the deprecated replay shim — and a digest that said different things on two of
    them would be two answers to one question.

    A disputed note is *marked in place* rather than listed separately, because the reader's
    question is "what is new" and the dispute is a property of an entry in that list. The trailing
    count is what a reader acts on: `kg/conflicts.py`'s own rule is that a silent truncation reads
    as completeness, and a list in which two of nine entries are marked says so out loud.

    **A line leads with what the note says and keeps the id beside it.** It used to be the id
    alone, which is a handle rather than a sentence — a subscriber was told
    `- playbook-aee3d30407cc` and had to go and look up their own digest. The id stays, in
    brackets, because it is what a reader types into `GET /notes/{id}` and what the watermark
    works in; a headline that replaced it would make the message prettier and unusable.
    `headlines` is optional so the deprecated replay shim, which has only ids, still renders.
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


def _is_new(note: Note, subscription: Subscription, arrived: date | None = None) -> bool:
    """Whether a note became knowledge after this subscriber was last told.

    `>=` rather than `>` on the date, because a note's `valid_from` is a *date* and the digest
    runs hourly: `>` would silently drop a note that appeared later on the same day the digest
    ran, which is the common case and the failure this feature exists to prevent.

    `>=` alone re-qualified every same-day note on every run — up to 24 deliveries a day, against
    `agent/subscriptions.py`'s promise that "asking twice does not double-notify" (DARK-7). The
    subscription therefore also remembers which ids it sent *at that date*, which separates
    "dated today and already sent" from "dated today and new" without having to choose between
    the two failures.

    **A note with no date was DARK-7 again, and the comment that used to sit below said it had
    been handled.** It read "report it once"; the branch returned `True` unconditionally, so a
    dateless note re-qualified on *every* run forever — the id memory could not help, since it is
    scoped to the watermark's date and resets when that rolls over. Measured on the shipped corpus:
    **32 of 39 notes carry no `valid_from`**, so a subscriber's hourly digest was mostly the same
    notes over and over, which is the failure the feature's own promise names.

    What `None` means is not a gap to be patched around: `Note.is_current` reads it as
    *open-ended* — true for as long as anyone has known — so a note carrying it is by definition
    not something that became knowledge after a subscriber was last told — about the *fact*. Whether
    the *note* is new to this subscriber is a different question, and the last paragraph answers
    it.

    **How much that consequence was worth was asserted here and never measured, and the number is
    the reason this paragraph was rewritten.** It used to read "a distilled rule is the one note
    type nobody writes on a day, and it was the note type this silence actually cost", which named
    `playbook` as the whole of it. Measured on the shipped corpus instead: **32 of 39 notes carried
    no `valid_from`**, spread over ten types — `compound` 9, `playbook` 5, `campaign` 3,
    `interaction` 3, `job-result` 3, `bo-candidate` 2, `failure-mode` 2, `optimization-campaign` 2,
    `report` 2, `experiment-proposal` 1. So the mitigation that shipped with this branch reached
    the memory miners and left every other producer silent, and a chemist watching "suzuki" who had
    already had one digest would never again be told that a report was drafted or that a connector
    job wrote its result.

    The producers closed part of that: `retrieval.harness.report_note` takes `drafted_on` and
    `durable.job_record.note_with_run_provenance` takes `ran_on`, each dating a note whose validity
    date and arrival date are the same day by construction. The rest is `arrived` — the date the
    note's file was committed to the corpus (`kg.graph.note_arrivals`) — which stands in for a
    missing `valid_from` and for nothing else. `agent.graph_tools.record_knowledge_note` is the
    case it exists for: the model may legitimately not know when a fact became true, and
    defaulting `valid_from` to today would trade this silence for a false claim about chemistry,
    where the arrival is simply true. With neither date — a corpus that is not a git work tree —
    the branch is what it was: not new.
    """
    when = note.valid_from or arrived
    if subscription.last_seen_at is None:
        # Nobody has been told anything yet, so everything matching is news.
        return True
    if when is None:
        return False
    if when > subscription.last_seen_at.date():
        return True
    if when < subscription.last_seen_at.date():
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

    `D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` generalised this into
    `durable/deliver_message.deliver_message_activity`, and deleting it outright was the defect:
    an in-flight `DigestWorkflow` has `ActivityTaskScheduled(deliver_digest_activity)` at this
    position, and the new code emitted `deliver_message_activity` there. Replayed, that is
    `[TMPRL1100] Nondeterminism error: Activity type of scheduled event 'deliver_digest_activity'
    does not match activity type of activity command 'deliver_message_activity'`. This workflow
    declares no `failure_exception_types`, so it does not fail — it **parks** its workflow task in
    an unbounded retry; and because the digest is a Schedule under `ScheduleOverlapPolicy.SKIP`,
    one wedged run then skips every subsequent nightly digest silently. The ADR's "nothing changes
    in a shipped deployment" was true of a fresh deployment and false of every live one.

    So the body is gone and the *name* stays. It is scheduled only on the `workflow.patched` off
    branch below, which no new run takes, and it delegates rather than duplicating: whatever a
    replayed run does here, it does through the one seam. Removable once no run opened before this
    release can still be replayed — for a nightly Schedule, the day after it ships.
    """
    return await deliver_message_activity(
        OutboundMessage(
            recipient=payload.owner,
            subject=f"New for your standing query: {payload.query}",
            # The shim has no `disputed` to render: a replayed run's recorded `DigestItem` predates
            # the field, so an empty list is the truth about that payload rather than a default
            # standing in for one.
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
            #
            # **Behind a patch**, because this position already holds an
            # `ActivityTaskScheduled(deliver_digest_activity)` in every run opened on the previous
            # release, and emitting a different activity type there parks the workflow task
            # forever — see `deliver_digest_activity` above for the measured error and why a
            # parked digest is silent rather than loud. The off branch emits exactly what those
            # histories record; no new run takes it.
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

    Public and imported by the reader (`api/routes/streams.read_digests`) rather than restated
    there: this string is the whole addressing scheme, so a second spelling of it would be a
    mailbox the job writes to and nobody opens — which is exactly the state
    `D-2026-08-27-a-digest-nobody-can-read-is-not-delivered` found.

    It is a *synthetic* session id: no `session_owners` row backs it, so it cannot be authorized by
    session ownership. The reader therefore never takes it from a caller — it derives it from the
    authenticated principal, which is why one chemist cannot name another's mailbox.
    """
    return f"digest-{owner}"
