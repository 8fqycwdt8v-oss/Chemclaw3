# D-2026-08-27-a-digest-nobody-can-read-is-not-delivered — the standing-query mailbox gets a reader, addressed by the caller rather than by the request

## Status

Decided and implemented (2026-08-27). Closes the `docs/planning/BACKLOG.md` row "The digest is
written to a mailbox with no reader, and the watermark advances anyway".

## Context

`durable/digest.py` re-runs each saved standing query on a cadence and pushes the new matches into
`session_events` under the synthetic session id `digest-<owner>`, kind `digest`. It then
acknowledges — `acknowledge_digest` → `mark_reported` — which moves that subscription's watermark
past exactly those note ids.

The only consumer of `session_events` in the tree was `GET /sessions/{id}/events`, which claims
`kinds=("job_completed", "job_failed")` and sits behind `resolve_session`. Neither half can reach a
digest: the gate 404s an id no `session_owners` row backs, and the claim filters the kind out.

## What was measured

Against a migrated Postgres in this checkout (the real writer, the route's own claim, the real
subscription store):

| probe | result |
|---|---|
| `claim_unconsumed("digest-repro-owner", kinds=("job_completed","job_failed"))` over a real digest row | `[]` |
| the row afterwards | `(id=1, kind='digest', consumed_at=None)` — untouched |
| `GET /sessions/digest-repro-owner/events` as that owner | `404 {"detail":"unknown session"}` |
| `_is_new(note, subscription)` before the ack | `True` |
| after `mark_reported(id, ["reaction-1"])` | `last_seen_note_ids=['reaction-1']`, `_is_new` → `False` |

So the delivery was a no-op and the watermark moved anyway, permanently: `_is_new` returns False
for a note whose id is in `last_seen_note_ids` at the watermark's date, and later dates are already
past. `durable/retention.py`'s `session_events` predicate is `consumed_at IS NOT NULL` — correct on
its own terms, and it made every digest row ever written immortal as well as unread.

The BACKLOG row's estimate of the blast radius is right and the ordering of the two harms is worth
stating: **matches were lost, not merely undelivered.**

## Decision

### 1. `GET /digests` is the reader, claiming `kinds=("digest",)`

One route, in `api/routes/streams.py` beside the job stream, because both are readers of the same
mailbox through the same kind-scoped claim. It returns the claimed rows as JSON.

**Not a second SSE stream**, though it would have inherited every bound the job stream has: the
digest cadence is `digest_schedule_minutes` (a day, by default), so a stream would hold one of the
caller's five per-user slots and a poller on the loop for hours to carry one row — and a digest is
not a turn event, so streaming it would want a member of the turn contract (`api/events.py`) that
no turn ever emits.

### 2. Authorization: the caller cannot name a mailbox

This is the hard half, and the answer is to remove the question rather than to answer it.
`digest-<owner>` is synthetic — no `session_owners` row backs it — so `resolve_session`, the gate
every session-scoped route uses, has nothing to authorize against. The two candidate designs were:

- a path segment (`GET /digests/{owner}`) checked against `principal.oid`: a gate that has to be
  *got right*, on a route whose whole content is another user's inbox;
- no id at all: the handler derives the channel from the authenticated principal with the writer's
  own `digest_channel`, exactly as `GET /sessions` scopes its listing to `principal.oid`.

The second is what ships. There is no owner in the path, the query string or a header, so reading
another chemist's digest takes a forged token rather than a crafted request, and the route needs no
entry in `tests/test_service.py`'s session-scoped inventory because it is not session-scoped — it
is *owner*-scoped, the shape `GET /sessions` and `GET /note-proposals` already have.

The two ends agree on the identity by construction, not by convention: `require_principal` binds
the principal into the identity context (`bind_request_actor` → `set_current_identity`), which is
what `require_actor()` returns to `watch_for`, which is what `subscriptions.owner` stores, which is
what the digest job addresses. `tests/test_digest.py::test_a_watch_is_owned_by_the_oid_the_route_reads`
pins that chain rather than restating it here. One consequence follows and is accepted: a watch
saved with *no* request in scope falls back to `service_actor_id`, and that owner has no front door
to read from — there is no such caller in the tree today, and inventing an operator surface for a
hypothetical one would be the mistake §3 declines.

`digest_channel` and `DIGEST_KIND` are now public in `durable/digest.py` and imported by the
reader. A second spelling of either is a mailbox one side writes to and the other never opens,
which is the defect above with extra steps.

### 3. `system-eval-drift` keeps no route, and the BACKLOG row's "same dead end" is only half true

Same absence of a route; not the same defect, and not a defect at all:

- **Nothing is lost.** That channel is written by `notify_session` (must-deliver, no watermark and
  no acknowledgement), so an unread alert costs visibility, never a match the system can no longer
  re-derive. `make eval-baseline-check` reproduces the same comparison offline.
- **Its consumer surface is already decided** — a WARNING on the operator's log path plus a
  documented SQL read (`docs/guides/runbook.md`, which says in as many words that no UI consumes
  this channel by design).
- **A claiming reader would destroy the record it exists to serve.** `retention.py` keeps a
  `session_events` row only while `consumed_at IS NULL`; the eval-drift rows' *non*-consumption is
  what stops the sweep deleting the evidence, and that comment names this channel explicitly. A
  route that consumed them would trade an unread alert for a deleted one.
- **There is no owner to authorize.** `system-eval-drift` is not anyone's `oid`; the identity that
  should read it is a role, so it would need an operator gate this route deliberately does not have.

So the digest gets a reader because a *user* is waiting for it and a watermark moves on its behalf;
the drift channel keeps its operator surface because neither is true of it.

### 4. The acknowledgement stays on the mailbox write — now justified rather than merely shipped

`acknowledge_digest` fires when `notify_session_best_effort` reports a committed INSERT, not when a
chemist reads the digest. That stays, and with a reader it is finally the right rule:

- The mailbox row is the durable handover. Retention will not age out an unconsumed row, so the
  digest waits for its owner indefinitely; the ack records "this is now someone else's to deliver",
  which is exactly what the insert established.
- Waiting for the *read* would mean a Temporal Schedule blocking on a human. Re-collecting an
  unread match instead would append a second row for the same notes every cadence, turning an
  inbox nobody has opened into a pile of duplicates — the DARK-7 failure `last_seen_note_ids` was
  built to prevent.
- The failure the ordering guards is still guarded: a swallowed delivery returns False and the ack
  does not run, so those notes re-qualify on the next pass.

What remains is the claim's own at-most-once window: a row claimed by `GET /digests` whose response
never reaches the client is not re-delivered. That is the contract this mailbox has documented since
COR-4, and here its cost is bounded to the *notification* — the notes are merged knowledge and the
query that found them is a saved subscription, so `list_watches` plus a search re-finds them.
Losing the notification is not losing the knowledge, which is precisely the distinction that did not
hold before this route existed.

### 5. `digest_enabled` keeps planning a Schedule

The BACKLOG row asked for `digest_enabled` to plan no Schedule *until a reader exists*. One exists,
so the flag stays an ordinary opt-in (default `False`, unchanged) and `planned_schedules()` is
unchanged. What changed there is the comment, which claimed the Schedule is earned "where someone
has subscribed" — a condition the code does not check — and now says what the flag actually turns
on and why enabling it no longer loses matches.

### 6. No new counter

`session_events.consumed_at` already records, per row, whether a digest was read — the same
evidence the runbook has operators read for the drift channel. A counter would need a declaration
in `core/metrics.py` to say less.

## Consequences

- A deployment that turns digests on now delivers them; before, turning them on lost matches.
- Digest rows become prunable once read, so the mailbox stops growing without bound. Unread ones
  still survive their window, deliberately.
- `_digest_channel` is renamed to `digest_channel` and gains a second caller; `DIGEST_KIND` replaces
  the `"digest"` literal at the write site.

## Files

`src/chemclaw/api/routes/streams.py` (the route), `src/chemclaw/durable/digest.py` (public channel
+ kind, docstrings that no longer claim a delivery), `src/chemclaw/durable/schedules.py` (the
comment), `tests/test_digest.py` (four tests: owner-only read, kind-scoped claim, consume-then-prune,
and the identity chain).
