# D-2026-09-09-a-sort-key-a-page-cannot-prune-is-a-scan — A sort key a page cannot prune is a scan

**Status:** accepted · **Date:** 2026-09-09 · Supersedes §2 of
`D-2026-08-10-a-list-of-ids-is-not-a-conversation-list` ("Last activity is derived, not mirrored")
and adjusts its §3, since the lateral is no longer the membership filter. That ADR is otherwise
unchanged and is not edited.

## Context

Two findings, one file, and they are the same shape: **a bound that holds at fixture scale and stops
holding at a real one.**

### The erasure's turn-claim guard stopped holding above ~3,500 sessions per actor

`leaver._sessions_held` claims the durable turn lease on every one of an actor's sessions before
erasing, and refuses if any is busy. Its docstring states what that prevents: a live turn writing
its in-memory history back onto a thread mid-erasure, leaving the pre-erasure conversation in the
database again in full, under a session id no later erasure can reach.

It claimed **one session per connection round trip**, measured at ~56/s, against a 60 s lease that
nothing refreshed. Driven at 600 sessions with the lease shortened so the hold-to-lease ratio is the
shipped one:

```
claim loop 10.67 s for 600 sessions;  held=562 expired=37 at loop end
held=486 expired=113 where the erase transaction runs
a simulated second pod TOOK sess-000001 at t+10.3 s, while the sweep was still running
```

The live turn the guard exists to refuse, admitted by the guard. At 6,000 sessions the finding
measured 40% of claims already expired before the erase transaction opened.

### The session sidebar cost O(everything the user had ever done), on every page

`_OWNER_LIST` took its sort key from a `LATERAL (SELECT max(created_at) …)`, so the planner had to
evaluate it for **every session the owner ever created** before it could discard any. 2 sessions
0.44 ms, 6,000 sessions 86.5 ms, 20,000 sessions 158 ms over 60,589 buffers. **The keyset cursor
did not help** — page 2 measured the same as page 1, because the cursor predicate is also on the
lateral's output and cannot prune the loop.

## Decision

**Batch the claims and refresh them while held.** One statement per batch over
`unnest(%s::text[])`, plus a heartbeat at `lease / 3`. Measured after: the 600-session loop runs in
**0.03 s** (22,081/s) with `held=600 expired=0` at loop end and still at t+12 s under a 10 s lease;
6,000 sessions take **0.51 s** against the ~104 s the old rate implies, and the second pod gets a
slot only after the sweep releases it.

Both halves are necessary and the ADR says why: batching alone still leaves the **erase transaction
itself** — 85 s for 400k rows — able to outlast a 60 s lease.

**Materialise `session_owners.updated_at`.** 20,000 lifetime sessions go from 158.36 ms / 60,589
buffers to **0.46 ms / 307 buffers**, and page 2 becomes flat.

## What the mirror costs, and how the old objection is answered

`D-2026-08-10` §2 refused the mirror on one argument: *"a second write per turn is a second thing
that can fall out of step."* That argument is sound and is not overruled — it is answered
structurally rather than by care, and what it lacked was a number for the alternative.

- **One definition.** The column *is* `max(session_messages.created_at)`, the same expression the
  lateral computed, substituted into both maintaining statements.
- **The write is in the same transaction as the message insert.** A mirror updated in a later
  transaction is exactly the drift §2 feared.
- **Membership never reads the mirror.** `_OWNER_LIST` keeps an `EXISTS (SELECT 1 FROM
  session_messages …)` arm beside the index condition, so a session whose rows retention pruned
  drops out the moment they go. **The mirror can mis-order a page; it cannot invent or hide a row.**
  Deletion is the only drift left and it is unobservable: rows prune oldest-first, so either the
  newest survives and the column is exact, or none does and `EXISTS` drops the session.
- **A third writer fails by name.** A scan in `tests/test_message_pairing.py`'s shape reddens the
  day a third `INSERT INTO session_messages` appears in `src/`.
- **Cost paid:** 0.068 ms / 6 buffers per turn over a 2,000-message session.

## Two hazards the design did not anticipate, found by building it

- **A naive `UPDATE … WHERE session_id = ANY(...)` refresh deadlocks against the erasure it is
  protecting.** It takes row locks on exactly the `session_turns` rows the erase transaction's own
  `DELETE` locks, in whatever order each plan chooses, and the victim can be the erasure. The
  refresh locks through `ORDER BY session_id FOR UPDATE SKIP LOCKED` — a row the erase transaction
  holds is a row nobody else can claim either, so skipping it costs nothing.
- **A refresh that warned on any row it did not get back would warn on every healthy erasure**,
  because the sweep's own transaction deletes those rows. Only a session a *different* holder now
  names is warned about, and it is then dropped from the heartbeat.

## What was disproved

The proposed index `(owner, updated_at DESC, session_id DESC)` does **not** flatten the shared dev
principal's page. Under the two-arm owner predicate a NULL parameter leaves `owner = NULL`
unindexable, so that page keeps its sequential scan and top-N sort: 146.19 ms → 67.72 ms at 20,000,
not → 0.46 ms. Stated in the migration and the test docstring rather than papered over.
`entra_required` deployments have no NULL owners.

## Left open

`_sessions_held`'s refusal joins **every** busy session id into one string. At fleet scale that
message is unusable and should be bounded.
