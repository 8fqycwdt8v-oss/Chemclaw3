# D-2026-10-17-a-hand-over-is-one-decision-and-the-line-names-its-turn — a queued message's hand-over is decided by its ticket and said on its stream, and the line names the running turn from Postgres

**Status:** accepted · **Date:** 2026-10-17 · **Works** issue #503 (item 9 and the review nits) and
the queue half of W7.12 in `docs/planning/programme-deferred-2026-10.md` · **Builds on**
`D-2026-10-01-a-queued-message-waits-in-its-senders-request`,
`D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line` and
`D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica`.

## Context

Building the shared-session client (Chemclaw3_ui, its `ISSUES.md` Issue 22) left three things the
client could only guess:

1. **The hand-over was not one decision.** A waiting message at the head re-checks its authority,
   takes the session's two claims, and only then deletes its ticket. A `DELETE
   /sessions/{id}/queue/{ticket}` landing in between found the ticket, deleted it and answered 204,
   and the message ran anyway. A 404 could mean "it already started" or "it was never there".
2. **The start was inferred** from the first frame on the sender's stream that was not a place in
   the line.
3. **`GET /sessions/{id}/queue` said only `running: bool`**, from this replica's registry or a
   relay probe, and named nobody, so a watched turn could not say whose it was until its exchange
   reached the transcript.

## Options

The hand-over:

1. **Removing the ticket is the decision.** `TurnQueue.leave` reports whether this call removed a
   live ticket. The waiter, holding both claims, removes its own; if a withdrawal removed it first,
   the waiter gives the claims back and ends on `queue_cancelled`. The withdrawal answers 204 only
   when it removed the ticket. One row, one winner, nothing new stored.
2. **Lock the ticket across the head's re-check.** That check may call the identity provider; a row
   lock held across it pins a pooled connection, which `D-121`'s lease exists to avoid.
3. **Keep a started ticket and answer 409 for it.** Withdraw could then say "it started" instead
   of 404, at the cost of a migration and a row that lives as long as the turn, to say what the
   sender's stream already says.

Saying it:

1. **A new kind, `turn_started {ticket}`, sent only on a hand-over.** Every other stream keeps its
   bytes; a client that does not know the kind drops it.
2. **Every turn opens with it.** A frame on every stream of every surface, for a fact only a queued
   message's client acts on.
3. **A sentinel on `queued`.** The one kind a client already reads two ways would get a third.

The running turn:

1. **Read it from Postgres:** the live `session_turns` claim (its `actor` and `claimed_at`) and the
   written-ahead question that claim covers (`COVERED`, read from the claim's side). One statement,
   the same answer on every replica; the question is absent until the turn has written it.
2. **Ask the holder** through the relay. A request row and a poll per read, on a route every open
   shared conversation polls every few seconds.
3. **Store the question on the claim.** `session_turns` is global (it counts leases across
   tenants), so a chemist's text there would sit outside every tenant policy.

## Decision

Option 1 in each.

- **Withdraw:** 204 means the message never runs; 404 means it is not waiting (it took the turn,
  was withdrawn already, or never existed). A lapsed ticket is already out of the line, so
  leaving it decides nothing, in both backends.
- **`turn_started`** is sent once, after the claims and the ticket are the message's and before
  anything the turn sends. A stream carries it or ends on `queue_cancelled`, never both.
- **`SessionQueueOut`** keeps `running` and adds `running_turn {sender, question, started_at}`
  (`None` exactly when `running` is false). A claim with no sender — a fork, a delete, an erasure
  holding the session — is not a running turn. Where sessions are in-process, the process answers
  from its own registry, which is then the whole deployment.
- The contract is 1.1.0 (an added kind and an added field).

The review nits ride with it: the waiter's process-ledger place moves to the turn slot in the same
synchronous step, so a message is counted once, not twice, while it takes the turn; every
`finally` does its in-process bookkeeping before any shielded await; and
`service_turn_watch_recheck_seconds` has a floor.

## Consequences

- `tests/test_session_turn_queue.py` holds the race (a withdrawal held inside the head's re-check
  wins, and the message never runs), the frame order, the single count against the per-actor cap
  and the in-process `running_turn`; `tests/test_turn_any_replica.py` holds one answer on two
  replicas; `tests/test_turn_write_ahead.py` holds the statement's cases.
- The UI reads `turn_started` and `running_turn` and pins this contract in the same release; it
  tolerates both missing, for an older service.
- The line is still polled. A push hint is the UI's open item, not this one.

Revisit when: a surface withdraws messages without holding their stream (so it never sees
`turn_started`) and needs Withdraw itself to answer "started" rather than 404 — then option 3 of
the hand-over.
