# D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line — what a waiting message is checked against when its turn comes

**Status:** accepted · **Date:** 2026-10-02 · **Works** issue #503 (the security review of #499,
items 1–6) · **Builds on** `D-2026-10-01-a-queued-message-waits-in-its-senders-request` (the message
waits in its sender's own request) and `D-2026-09-27-in-a-shared-session-the-sender-governs` (a turn
runs as its sender; membership is reach).

## Context

`D-2026-10-01-a-queued-message-waits-in-its-senders-request` rejected a dispatcher because a role
set copied into a table would survive "a role revocation between send and run". The design it chose
avoids the table and keeps the gap: the waiting *request* carries the principal its POST
established, and only membership was re-read at the head of the line. A wait can last about
`service_turn_queue_max` × `service_turn_timeout_seconds`, long enough for the token to expire. The
review found four related gaps in the same work: the per-actor cap ignored waiting messages, watchers
could exceed their cap and the per-user stream cap, the socket budget did not cover waiters whose
turn runs on another replica, and nothing required the poll to be shorter than the lease it
refreshes.

The choice that needed deciding is **what "current authority" means at the head**. Roles here come
only from the token (`api/auth._principal_from_claims`); there is no directory lookup, and D-089
forbids the Graph call one would need.

## Decision

**At the head of the line, a waiting message is admitted again on everything its POST was admitted
on, and refused with `queue_cancelled` (which says why) if any no longer holds**
(`api/routes/turns._refusal_at_the_head`, asked on every look at the head):

1. **Membership**: unchanged, through `_resolve_session`.
2. **The credential**: `api/auth.reauthorize` runs the same validation (signature, audience,
   issuer, `exp`) on the same bearer token. An expired token, or one naming somebody else, cancels
   the message. An unreachable key set cancels it too: the check fails closed. The turn then runs
   with the principal this validation returns. Because roles come only from the token, a role
   revoked in the tenant takes effect here when the token carrying it expires. That is the bound
   for every request this service serves, and it now holds for a queued one as well.
3. **The per-actor cap**: a message waiting in another session counts against
   `service_max_concurrent_turns_per_actor` at admission (`api/state._waiting_besides`). The
   sender's running turns are counted again at the head, because a burst of concurrent POSTs can
   pass the first check together.

**Bounds on the waiting machinery.**

- **Waiters per process** are capped at `service_max_concurrent_turns` × `service_turn_queue_max`
  (429 past it). The socket backstop in `core/config/__init__.py` charges exactly that product. It
  used to charge waiters per local turn, which missed every waiter whose turn ran on another
  replica.
- **A watcher counts until its socket closes** (`api/detach.Watch`), not only until the pump stops
  feeding it. A watch also takes one of the watcher's `service_max_event_streams_per_user` slots
  (`api/state._take_event_stream_slot`, shared with the push-back stream).
- **Membership is read again while watching**, before an event once
  `service_turn_watch_recheck_seconds` have passed. A removed member's view closes **without a final
  event**, because a stranger is told nothing.
- **`service_turn_queue_poll_seconds` must be below `service_turn_claim_lease_seconds`**, or startup
  refuses. At or above the lease, every ticket lapses between two polls.

## Alternatives weighed

- **Record the bound as a known limit and change nothing.** Rejected. The table half of the
  10-01 ADR already promised that nothing outlives the token that authorized it, and re-checking
  the token is one `validate_token` call at a point the request already pauses at.
- **Re-enter `require_principal` at the head.** Rejected. It is the request funnel: it spends the
  caller's rate budget and binds ambients, so a waiting message would be charged for waiting and
  could be cancelled for a rate its sender never exceeded.
- **Fetch the sender's current roles from the directory at the head.** Declined. It needs a Graph
  call, which D-089 does not permit, and no other request path has one. A queued message would be
  the only thing in the service with fresher roles than a direct turn. **Revisit when:** a design
  gives *every* request current roles (a role source other than the token, such as a revocation
  list the front door reads), since a queued message would then use it through `reauthorize` with
  no change here.
- **Let a message over its sender's cap keep waiting at the head.** Rejected. The head blocks the
  whole line, so one sender's other turns would stall every participant behind them.
- **End a removed watcher's view with an error event.** Declined, because it needs a new `ErrorCode`
  member and so a new wire contract for a case where the honest answer is nothing.
  **Revisit when:** `Chemclaw3_ui` reports that a silently ended view cannot be told apart from a
  dropped connection in a way that matters to the person removed.

## What was measured rather than assumed

`tests/test_session_turn_queue.py`, over a real uvicorn server on loopback:

- With real RS256 tokens and nothing patched in the seam, a message whose token expires while it
  waits ends with `queue_cancelled` and never reaches the model.
- A sender over their cap at the head is cancelled.
- A waiting message counts against its sender's cap in another session.
- A full process is refused 429.
- A watcher cut off for lagging still counts until its stream closes, and a further watch is
  refused 429.
- A watch takes a stream slot and gives it back.
- A member removed mid-turn stops receiving.

`tests/test_service.py` checks the formula and the poll-under-lease refusal.
