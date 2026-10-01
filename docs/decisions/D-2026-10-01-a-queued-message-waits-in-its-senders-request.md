# D-2026-10-01-a-queued-message-waits-in-its-senders-request — a shared session queues, fans out, and finds members' plans

**Status:** accepted · **Date:** 2026-10-01 · **Closes** the `BACKLOG.md` row *"A shared session
serialises by refusing and streams to one reader"* (issue #488) · **Builds on**
`D-2026-09-27-in-a-shared-session-the-sender-governs` (a turn runs as its sender; membership is reach,
not authority), `D-121` (the per-session claim), `D-166` (a wait is reported on the stream) and
`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` (a response is a view of a pump).

## Context

With several people in one session, three single-person assumptions broke:

1. A message sent while another participant's turn runs was refused 409 by the session's turn claim.
   The serialisation is right — two turns must never interleave one thread — but the answer is not:
   the second chemist's question is not a double-submit.
2. `api/detach.DetachableTurn` had one queue and one reader, so a second participant reattaching
   would *steal* events from the first rather than see a copy.
3. `GET /plans/pending` paged only the caller's *owned* sessions, so a plan a member's turn wrote in
   somebody else's session — which only that member may decide — reached them only through the
   in-turn card.

The choice that needed deciding rather than just building is **where a waiting message lives**,
because this repository's rule is that durability lives in Postgres and Temporal and never in the
conversation layer's own stores.

## Decision

### 1. The line's *order* is in Postgres; the *message* stays in its sender's request

`session_turn_queue` (migration 113, `agent/session_queue.py`) holds, per waiting message, a session,
a sender, an identity-column ticket and a lease — **no text and no roles**. The waiting happens in the
sender's own `POST /sessions/{id}/messages`: its stream reports `queued` with the ticket and the
place, and when the ticket is at the head and the session's two existing claims (the in-process slot
and the durable `session_turns` row) come free, *that same request* takes them exactly as an
uncontended turn does and runs `run_turn` with the principal its own token established. So:

- **A queued turn runs as its sender**, by construction — there is no dispatcher that could run it
  as anybody else, and no table holding an authority that could outlive the token that granted it.
- **Membership is re-checked at the head** through the same `_resolve_session` gate every request
  passes, so a member removed while waiting does not have their message run (`queue_cancelled`).
- **Order is the database's admission order of tickets**, shared across replicas; a message never
  jumps a line that exists (the fast path is taken only when nobody is waiting).
- **Bounded twice**: `service_turn_queue_max` live tickets per session, and one per sender per session,
  both checked under a per-session advisory lock in one transaction. Past either, the POST is refused
  409 with a detail naming which limit.
- **A dead waiter costs at most one lease**: the waiter refreshes its row on every poll; a lapsed row
  stops counting as ahead of anybody and is swept by the next enqueue. The table cascades from
  `session_owners` and the leaver erases a person's tickets, so deletion, retention and erasure all
  reach it.
- **Withdrawal**: `DELETE /sessions/{id}/queue/{ticket}` — the sender for their own, the owner for any
  (the stop route's rule, one step earlier); a member withdrawing somebody else's is 403, a ticket
  this session does not hold is 404. The waiting stream ends with `queue_cancelled`.

**The durability this does and does not claim.** The order is durable and cross-replica. A waiting
message is exactly as durable as a POST in flight: if its sender's request or its pod dies before the
turn starts, the message is gone, its ticket lapses, and the sender's stream has ended — nothing
claims otherwise. Once it runs, the turn's durability is the ordinary one (checkpointer, transcript).
No in-process structure is a store of record: `InMemoryTurnQueue` exists only for
`session_store="memory"`, where the session itself is in-process.

### 2. Fan-out: a reader per participant, none of whom can hold the turn

`DetachableTurn` keeps a set of readers, each with its own bounded buffer; the pump offers every event
to every reader with `put_nowait` and never awaits one. A reader whose buffer fills has stopped
reading and is cut off with one `stream_lagged` error (`chemclaw_turn_readers_lagged_total`), so a
stalled browser can slow neither the turn nor anybody else's view. `GET /sessions/{id}/turn/stream`
attaches a further participant: the session gate admits owner or member (a stranger gets the same 404
an unknown id gets), the turn is resolved from *this* session's registry entry, and
`service_turn_max_watchers` bounds the readers (429 past it). Only the sender's reader leaving is a
detach; a watcher leaving changes nothing. A late joiner sees events from the moment it attaches —
the stream carries no ids to replay from, and the transcript has the rest.

### 3. The pending-plans inbox includes plans the caller authored in sessions they are a member of

The inbox scans the caller's owned sessions and the sessions `GET /sessions/shared` lists, newest
activity first across both, under the same `service_max_plan_scans` budget. In a member's session a
plan is listed only when the caller is its recorded author — never the owner's plan and never an
unattributed one, which the owner decides — so an inbox row never answers 403 when decided.

## Alternatives weighed

- **Persist the message and its sender's roles, and run it from a dispatcher.** Rejected: the turn
  would run on a role set copied into a table, outliving the token that authorized it and surviving a
  role revocation between send and run — the shape `D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned`
  refuses — and the message text would become one more erasable store. **Revisit when:** a deployment
  needs a queued message to survive its sender closing the tab before it starts, and a design names
  how the dispatcher obtains the sender's *current* roles at run time.
- **A Temporal workflow per queued message.** Rejected for the same reason plus layering: a turn is
  layer 1's work (LangGraph over a per-turn graph), and moving its start into Temporal would put a
  conversation step in the durable-execution layer. Temporal stays the home of long work a turn
  starts, not of the turn.
- **An in-process queue only.** Rejected: two front-door replicas share a session, so the order must
  be shared; a per-process line would let a message on replica B overtake one waiting on replica A.
- **A shared buffer with back-pressure for all readers.** Rejected: the slowest participant would set
  the pace of everybody's view and of the turn — exactly what the per-reader cut-off prevents.
- **Watching a turn running on another replica.** Not built: the pump lives in one process, so the
  watch route answers 404 there, the stop route's scope. **Revisit when:** a deployment runs
  front-door replicas without session affinity and participants report missing live views — the fix
  is a cross-process event relay (e.g. Postgres `LISTEN/NOTIFY`), not a change to this decision.

## What stays a stated limit

**An erasure still cannot cut a member's words out of the owner's checkpointed graph state** — the
limit `D-2026-09-27-in-a-shared-session-the-sender-governs` named, kept in `agent/leaver._BEYOND_REACH`.
A queued turn adds nothing new to it: it writes the same thread the sender's direct turn would. It goes
when the owner deletes the session or leaves.

## What was measured rather than assumed

`tests/test_session_turn_queue.py`, over a real uvicorn server on loopback: a message sent during
another participant's turn waits in order and runs with *its sender's* oid and roles read from inside
the running graph; a stranger can neither join, read nor withdraw from the line, nor watch the turn
(404 on each); a member cannot withdraw another's message (403) while its sender and the owner can; a
member removed while waiting does not run; a full line and a second ticket from one sender are 409;
every participant follows the running turn and a stalled watcher is cut off without holding the turn;
both queue backends honour one contract and the durable one goes with its session; and the inbox lists
a member's own plan in a shared session but neither the owner's plan there nor a member's plan in the
owner's inbox.
