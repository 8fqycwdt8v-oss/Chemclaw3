# D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica — any replica follows and stops a running turn, through rows its holder polls

**Status:** accepted · **Date:** 2026-10-04 · **Builds on**
`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` (a response is a view of a pump; a stop is a
request), `D-2026-10-01-a-queued-message-waits-in-its-senders-request` (any participant follows a
turn; a turn is its sender's or the owner's to stop) and
`D-2026-10-03-an-unload-stop-waits-for-a-reload` (an unload stop waits for a reload). **Fires the
triggers** of the "Watching a turn running on another replica" decline in the first and the
"Cross-replica deferral" decline in the second: both named this condition — replicas without
session affinity — and both named the fix as a cross-process relay.

## Context

A running turn's pump, its readers and its cancel live in the memory of the process that started it
(`api/detach.DetachableTurn`, registered in that process's `RunningTurns`). Measured with two
front-door processes running `create_app()` on one database, `CHEMCLAW_SESSION_STORE=postgres`,
against `chemclaw.cli.mock_llm`'s `[[f-slow]]` turn started on A:

- `GET /sessions/{id}/turn/stream` on B → **404** `no turn is running for this session`;
- `POST /sessions/{id}/turn/stop` on B → **404**, and the turn ran on A to its answer.

The Route pins a browser to one pod with a cookie, but `Chemclaw3_ui`'s BFF reaches the front door
through the Service (`chemclaw-service:8080`), where no cookie exists. So on any deployment with more
than one front-door replica — the chart's floor is two — about half of every reattach and half of
every Stop landed on the wrong pod: the live view vanished and the Stop button did nothing, while the
answer still reached the transcript. The unload-stop deferral had the same hole one step later: a
reload reattaching on the other replica could not cancel the stop pending on the holder.

## Decision

**A replica that does not hold the turn asks the one that does, through two leased tables its
holder polls** (`infra/sql/121_session_turn_remotes.sql`, `agent/turn_remotes.py`,
`api/turn_relay.py`).

- **The request.** The asking replica writes a `session_turn_remotes` row naming the session, the
  claim's `holder` (`session_turns.holder`, so the request is answerable only by that turn — the next
  turn on the session has another holder and never sees it), what it wants (`watch`, `stop`,
  `unload_stop`) and who asks. It refreshes the row's lease while it waits; an asker that dies stops
  refreshing and the holder sweeps the row after one `service_turn_relay_lease_seconds`.
- **The holder** runs `TurnRelay.run` for the life of the process. Every
  `service_turn_relay_poll_seconds`, **while it holds a turn**, it reads the requests addressed to its
  own claims in one statement and answers each with the call its own route makes: a follow attaches
  an ordinary `DetachableTurn.watch` view and `resume`s, a stop calls `stop()`, an unload stop calls
  `defer_stop()`. So the watcher cap, the lag cut-off, the draft coalescing, the unload grace and its
  "is the sender watching at expiry" read count a remote view exactly as they count a local one —
  there is one turn object and one rulebook, not a second implementation.
- **The frames.** A followed view is drained as fast as the turn produces and written to
  `session_turn_frames` once per poll; the asker consumes them by deleting them, one round trip per
  poll. `NULL` ends the view. A backlog the database is not taking is cut off as `stream_lagged`, as
  a stalled local reader is.
- **Authorization stays on the asking side and is unchanged.** Both routes are behind the same
  session gate (a non-participant is 404 before anything is written), and the stop route's
  sender-or-owner rule is applied from the sender the claim now records (`session_turns.actor`,
  written by `claim`) — a claim with no recorded sender, taken by the previous image, is treated as
  somebody else's turn, owner only.
- **The answers.** Follow: 200 with the turn's correlation id, 429 when the holder's turn is at its
  watcher cap, 404 when it ended. Stop: `{"stopped": true}` once the holder's teardown has run,
  `{"stopped": false, "deferred": true}` for an unload stop, 404 when it ended first. A holder that
  does not answer within the lease (an old image mid-rollout, a holder starved of its loop) is a
  **503** with `Retry-After`, never a silent 404. A followed view whose holder died ends without a
  final event, as a local view's socket does, and the reattach then answers `410 turn_interrupted`.

Measured after, same two processes: Stop sent to B answered `{"stopped":true}` in **0.28 s** and the
turn on A ended without its answer; B's follow received every frame from attach to the same `answer`
event A's sender received, under the same `X-Chemclaw-Turn-Correlation-Id`.

## Options considered

- **Keep the affinity requirement and route by session at the client.** Declined: the BFF is a
  shared server-side client with no cookie, and a Service has only ClientIP affinity, which would
  pin every chemist behind one BFF pod to one backend pod and defeat the HPA. And it would leave
  the turn reachable from one pod only, which a rollout or an HPA scale-in breaks anyway.
- **Proxy pod-to-pod.** The non-holding replica forwards the request to the holder's pod address.
  Declined: it needs a pod-addressable identity in the claim, a NetworkPolicy allowing front door to
  front door, an inter-pod credential, and a second HTTP hop in the SSE path — a new network surface
  for a problem the shared database already solves with the lease semantics `session_turns` has.
  **Revisit when:** measured relay load on Postgres (the rate of `session_turn_frames` inserts, or
  `chemclaw_pg_pool_*` waits attributed to it) becomes the front door's bottleneck.
- **Postgres `LISTEN/NOTIFY`.** Declined for the frames: a NOTIFY payload is capped at 8000 bytes,
  and an `exhibit_draft` frame carries a whole document (up to `exhibit_max_spec_bytes`), so frames would
  need chunking and reassembly; and LISTEN holds a dedicated connection per process for its
  lifetime, outside the pool every other statement here borrows from. This codebase already takes
  the polling side of that trade for the session line (`service_turn_queue_poll_seconds`).
  **Revisit when:** the relay poll's latency is the complaint — a remote view measured visibly
  choppier than a local one at the shipped `service_turn_relay_poll_seconds` — at which point a
  NOTIFY used only as a *wake-up* (no payload) in front of this same table is the change.
- **Persist every frame of every turn and tail it.** Declined: it writes every token of every turn
  whether or not anybody watches from elsewhere. Here frames are written only for a view that was
  asked for, and only while it is open.

## Consequences

- No session affinity is needed for a running turn. The Route keeps its cookie for as long as
  anything else is per-pod (uploaded attachments, on `main` at the time of writing), and the
  `templates/service-route.yaml` comment says which.
- A replica holding a turn issues one statement per poll while it holds one; an idle replica issues
  none. A remote view costs its asker one round trip per poll and its holder one insert per poll.
- A remote Stop takes up to one poll to reach the turn; a remote view arrives in poll-sized chunks.
- Two settings: `service_turn_relay_poll_seconds` and `service_turn_relay_lease_seconds`, the second
  strictly above the first (refused at startup otherwise).
- `chemclaw_turn_relay_poll_failures_total` counts the holder's failed polls.

## What keeps it true

`tests/test_turn_any_replica.py` drives two `create_app()` instances under two uvicorn servers over
one migrated database: a follow from B sees A's turn to the same ending (also with each relayed write
slowed past several polls, the window a first version lost the answer in), a Stop from B ends the
turn and settles its question `stopped` (also with the holder's `stopped` answer landing after the
turn's claim is released, which a first version reported as 404), a member is 403 and a non-participant 404 on B, an unload
stop on B is deferred and the sender's reattach on B cancels it, and a request addressed to an ended
turn is invisible to the next one.
