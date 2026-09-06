# D-2026-09-05-a-push-nobody-claims-is-not-a-push — `awaiting-answer` reaches the session stream, and the contract gains the event that carries it

## Status

Accepted. The third instance of the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` named and
`D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` applied — a control that exists, is asserted by
a test, and is not reached by what it is for. This one resolves the other way: the producer is
sound and wanted, so what is added is the consumer, not a deletion.

## Context

`AwaitAnswerWorkflow` is how this system stops and asks a person something only a person can answer
— a campaign's measurement, an effect's approval, a question raised by a workflow. It has always
pushed back into the requester's mailbox. `_push` writes an `awaiting-answer` row into
`session_events` on the open, on every reminder, and again on expiry, and its docstring argues
carefully about the one case where it must *not* (a wait with no session).

Nothing has ever read those rows.

```
$ grep -rn "notify_session_best_effort(" src/ --include='*.py' | grep -v "def \|import"
src/chemclaw/durable/awaiting.py:398        AWAITING_KIND
src/chemclaw/durable/digest.py:272          digest kind
src/chemclaw/durable/connector_job.py:903   "job_failed"
src/chemclaw/durable/connector_job.py:967   "job_completed"
src/chemclaw/durable/template_job.py:254    "job_completed"
src/chemclaw/durable/template_job.py:322    "job_failed"
```

Five kinds are produced. `GET /sessions/{id}/events` claimed exactly two of them:

```python
async for pushed in front_door.stream_new_events(
    session_id, kinds=("job_completed", "job_failed")
):
```

`DIGEST_KIND` has its own claim on its own channel (`streams.py`, the `/digests` route).
`AWAITING_KIND` had **no claim anywhere**. So every notification this workflow has ever written was
written and never delivered, and the claim is destructive and at-most-once (COR-4), so there was
never a second chance at it either.

**And they are all still there.** An unclaimed row is immortal twice over: `durable/retention.py`
prunes `session_events` only `WHERE consumed_at IS NOT NULL`, and `retention_session_events_days`
defaults to `0` (disabled) with no value in `.env.example` or `deploy/`. `durable/digest.py` already
says so in the present tense about its own kind — *"which `durable/retention.py` then declines to
prune forever"*. So widening the claim does not deliver *the* notification; it delivers the entire
history on the first connect. Measured on one BO campaign opened, chased daily and expired a month
ago: **16 frames on a single poll**, fifteen of them `waiting` for a question that is closed.

**What a chemist saw instead was worse than nothing.** `agent/pending_tools.py:106` records
`record_job_started(handle.id, "awaiting")` beside the wait, which arrives on the turn stream as a
`job_started` carrying `kind="awaiting"` — a kind no surface knows (`grep -rn awaiting
Chemclaw3_ui/src Chemclaw3_ui/shared` finds only `approval_request`, an unrelated event). A
`job_started` with no matching `job_completed` renders as a durable job. The wait's default deadline
is days; the job appears to run for them and then to vanish. The one *correct* reading — "you were
asked a question and it expired unanswered" — is the one reading the stream could not produce.

## Decision

**Widen the claim to three kinds and declare the event that carries the third.**

`AwaitingAnswerEvent` joins the `Event` union in `api/events.py`; `streams.py` claims
`("job_completed", "job_failed", AWAITING_KIND)` and maps a row of that kind through
`_awaiting_event`.

Four things about the shape, each of which could have gone the other way:

**One event, not two, with `state` telling them apart.** The two pushes differ in three fields:
the open and every reminder add `kind`, `asked_of` and `due_at`, which the expiry omits. Everything
else — `request_id`, `subject`, `state`, `reminders` — is on both. Two event types would put the
choice of which to parse on every consumer; one type with `state` puts it on one `if`, and `state`
is a field the payload already carries.

**Every field but `request_id` is defaulted, and the read is `.get`, not `[]`.** The row is
*already claimed* by the time `_awaiting_event` runs — the claim is the destructive act, so there is
no re-delivery and a `ValidationError` here does not retry the notification, it destroys it. The
request itself is still open, still in `GET /pending`, still on its deadline; losing the notice of
it is the whole harm this ADR exists to end, and it would be perverse to reintroduce it as a
validation failure. `_digest` is lenient for the same reason and this is the stronger case.
`reminders` is taken only when it is already an `int`, and defaulted otherwise. Not `int(...)`,
which is what the first draft of this used and is not lenient at all: `int("many")` raises, and a
raise inside the mapper is strictly worse than the `ValidationError` it replaces — the generator
dies, every event queued behind the bad row dies with it, the handler books it on
`chemclaw_db_unavailable_total` (the counter that tells an operator a Postgres outage from anything
else), and `restore_unconsumed` returns the poisoned row so the client retries into the same crash
for ever while its batch-mates are already consumed and gone.

**`AWAITING_KIND` is imported from `durable.awaiting`, not re-spelled.** A wire constant with two
spellings is drift a route cannot notice — this fleet's own
`tests/test_identity_contract.py` exists because a header name was spelled twice and both were
wrong.

**Widening the claim steals from nobody.** `claim_unconsumed` is kind-scoped precisely so a
selective consumer leaves other kinds for theirs, and this kind had no consumer to take it from.
`tests/test_service.py::test_pushback_streams_a_question_waiting_on_a_person` asserts both halves —
that `AWAITING_KIND` is in the claimed tuple *and* that the two original kinds are still there.

**The stream reports each request's state, not its log.** Given the backlog above, that is what
makes the widening safe to turn on: a reminder carries no fact its open did not — this request is
open — so a repeat of a state already sent on this connection is suppressed, and the transition that
matters (`waiting` → `expired`) is always delivered. Per connection rather than per row, because a
reconnect is a fresh surface that needs the current state again. Sixteen frames become two.

## Consequences

**The contract tripwire fired, which is the cross-repo half of this change.**
`tests/fixtures/turn_events_contract.json` is a golden file over the whole union, and its failure
message is the instruction:

> This shape is mirrored by hand in `Chemclaw3_ui`'s `shared/events.ts` — the interface AND
> `normalizeEvent`, which rebuilds every event field by field — so a field that does not reach it
> is **DROPPED in transit** rather than merely ignored.

So this backend change is inert on its own: a UI that does not mirror `awaiting_answer` receives
the event and discards it, which is the same outcome as before by a different mechanism. The mirror
lands in the same change, in that repository, as its own pull request — a companion-repo change is
never proxied through this one.

**A deployment that has been running waits replays its backlog on the first connect after this
ships**, collapsed to one notice per request per state by the rule above. Nothing was lost — the
rows were never pruned — so this is a behavioural change to argue for rather than a recovery: the
first chemist to open a session with old waits sees the open questions it still has, and one expiry
line per question that has since closed. `GET /pending` remains the authority on what is actually
answerable; this stream is a notification.

**`request_id` is the `job_started` id**, because `record_job_started(handle.id, …)` and
`handle.id == request_id_for(request)`. A surface can therefore settle the standing "job started"
row against this event's expiry rather than leaving it running for ever, which is the defect the
paragraph above describes and nothing else in the tree says is closeable.

**`record_job_started(handle.id, "awaiting")` is left alone.** It is not wrong — a wait *is* a
durable job and the id is how it is answered — and now that the stream carries the wait's own
lifecycle, the two events describe one thing from two angles rather than one thing with its ending
missing. Removing it is a separate argument nobody has made.

**What this does not do is decide how a surface renders it.** The event says a question is open,
who it was asked of, and when it is due. Whether that is a badge, an inbox row or an interruption is
the UI's decision, and the deadline is the only field this repository has an opinion about.
