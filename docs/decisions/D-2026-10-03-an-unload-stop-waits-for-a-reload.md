# D-2026-10-03-an-unload-stop-waits-for-a-reload — a page's unload stop is deferred, and a reattach cancels it

**Status:** accepted · **Date:** 2026-10-03 · **Builds on**
`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` (a response is a view of a pump; a stop is a
request) and `D-2026-10-01-a-queued-message-waits-in-its-senders-request` (any participant can
follow a turn through `GET /sessions/{id}/turn/stream`; a turn is its sender's or the owner's to
stop). Found by a real-browser end-to-end run of the full system on kind (defect D5,
`Chemclaw3_ui` issue #131).

## Context

Reloading the page mid-turn killed the turn, and the reloaded page then waited up to 630 s for an
answer that was never coming. The server log of one reload, in order: *"went away mid-turn; the
turn continues detached"* → `POST /sessions/{id}/turn/stop` → *"stopped by request"* → the turn
ends abandoned → eight `GET /sessions/{id}/messages`, each `[]`.

Two client behaviours, each right on its own, were fighting. `Chemclaw3_ui`'s `pagehide` handler
stops the turn of a document being discarded, because a detached turn otherwise holds a per-actor
slot, a database connection and an LLM bill for up to `service_turn_timeout_seconds` on an answer
nobody reads. And its reload path (`resumeInterruptedTurn`) goes looking for that same answer. The
browser **cannot tell a reload from a closed tab at unload time** — `pagehide` carries no such
fact, and `PerformanceNavigationTiming` reports a reload only on the *next* load — so the client
cannot choose between the two; whatever it sends at unload is sent for both.

## Decision

**A stop sent from unload is deferred, and the sender coming back cancels it.**

- `POST /sessions/{id}/turn/stop?reason=unload` marks the running turn *stop pending* and answers
  `{"stopped": false, "deferred": true}`. The turn is cancelled after
  `service_turn_unload_grace_seconds` unless the turn's sender — or the principal who asked for the
  stop — reattaches with `GET /sessions/{id}/turn/stream` first; that reattach cancels the pending
  stop and the turn runs on with the reloaded page watching it (`DetachableTurn.defer_stop` /
  `resume`).
- **Without `reason`, a stop is immediate**, exactly as before — including over a pending
  deferral. The Stop button never waits.
- **Authorized exactly as an immediate stop is**: the same session gate and the same
  sender-or-owner rule, checked before the reason is read, so the deferral grants nothing an
  immediate stop did not. Only the sender or the requester can cancel it; another participant
  opening a view leaves it pending.
- **The window cannot be used to keep a turn alive.** One window per stop — a second unload stop
  while one is pending does not move its deadline — and at most
  `service_turn_unload_grace_max_deferrals` deferrals per turn, past which an unload stop is
  immediate, so a reload loop cannot restart the window for ever. In any case a deferred turn is
  never kept alive *longer* than doing nothing would: without the stop a detached turn runs to its
  own end, and `service_turn_timeout_seconds` keeps ticking inside the pump throughout.
- **Every cap still counts it.** The turn keeps its lease, so the per-actor cap and
  `chemclaw_turns_in_flight` count it during the window exactly as they count any detached turn;
  its admission permit already went back at the detach.
- **Observable**: `chemclaw_turns_stop_deferred_total`, `chemclaw_turns_stop_resumed_total`,
  `chemclaw_turns_stop_expired_total` (an expiry also counts in `chemclaw_turns_stopped_total`),
  and one log line at each of the three moments.
- **A watch response names the turn it is a view of** (`X-Chemclaw-Turn-Correlation-Id`, the id
  the sender's own `POST …/messages` response carried). The watch request's own
  `X-Chemclaw-Correlation-Id` names the watch, and in a shared session the turn running when a
  page comes back may be another participant's that started meanwhile — following that one would
  put their work under this chemist's question. The reloaded page follows only its own turn.
- `service_turn_unload_grace_seconds=0` restores the immediate unload stop exactly.

`Chemclaw3_ui` sends the reason from its unload path, and after a reload reattaches through
`GET /sessions/{id}/turn/stream` rather than polling the transcript; a 404 there (the turn is
over, or runs on another replica) falls back to a bounded transcript read. Against a core without
this decision the reason is an unknown query parameter, ignored, so the UI change is safe to land
first.

## Alternatives weighed

- **Stop nothing at unload; let every unloaded turn run detached.** That is
  `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` with the UI's handler removed. It fixes the
  reload and reopens what the handler was written for: a chemist who closes the tab holds their
  per-actor slot and pays for the whole turn. Rejected; the deferral keeps the reload and gives the
  capacity back a grace window after a real close.
- **Decide in the browser.** Persist an "unloading at T" marker and let the next load decide
  whether to stop. Rejected: a closed tab has no next load, so the turn that most needs stopping is
  the one this never stops.
- **A re-POST of the message resumes the turn.** Rejected: a re-POST during a running turn already
  means *queue behind it* (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`), and
  overloading it to also mean *I am the page that left* would make one request carry two
  meanings, which is exactly the confusion `D-2026-08-27` separated. The watch route is already
  the sender's way back to a running turn.
- **A body (`{"reason": "unload"}`) rather than a query parameter.** Equivalent on the wire; the
  query parameter needs no request model on a route that has none, and both an older core and the
  UI's offline contract check ignore it, which is what lets the UI land first.
- **Event ids and replay on reattach**, so the reloaded page sees what streamed while it was gone.
  Not built: the turn's final `answer` event is the whole answer, which is what makes the gap
  survivable, and the transcript holds the rest. **Revisit when:** a chemist-facing report shows
  the partial work between unload and reattach (tool calls, plan steps) is missed rather than
  merely the token stream — the change is a per-turn ring buffer and `?after=` on the watch route,
  the row `docs/planning/DEFERRED.md` already holds for streaming reattach.
- **Cross-replica deferral.** The pending stop lives on the pump, which lives in one process — the
  stop route's and the watch route's existing scope. A reload that lands on another replica gets a
  404 from the watch route and recovers from the transcript; the stop it left behind expires on its
  own. **Revisit when:** front-door replicas run without session affinity and reloads are measured
  losing turns to the expiry — the fix is the same cross-process relay the watch route would need.

## Consequences

- A reload inside the window continues the turn and the reloaded page renders it to its answer.
- A real close costs at most one window of the turn's spend before it is stopped, where it used to
  cost none; with the default window that is seconds of a turn whose deadline is minutes.
- `tests/test_unload_grace.py` drives each claim over a real socket: a reload inside the window
  continues; no reattach stops the turn after the window; an explicit stop is immediate even over a
  pending deferral; another participant's view does not cancel it; the watch response names the
  sender's turn; repeated unload stops neither
  move the deadline nor exceed the cap; and the counters record each outcome.
