# D-2026-10-23-a-relay-wait-is-woken-not-polled — the cross-replica turn relay waits on a `NOTIFY` wake-up in front of its rows

**Status:** accepted · **Date:** 2026-10-23 · **Builds on**
`D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica`, whose `LISTEN/NOTIFY`
decline named this change as its revisit ("a NOTIFY used only as a *wake-up* (no payload) in front
of this same table"), and
`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint` (a `LISTEN` dials the
session-store DSN).

## Context

The relay polled every `service_turn_relay_poll_seconds` (0.25): the holder read its requests while
it held a turn, a remote view read its frames, and an asker read its answer. Measured with two
front-door processes on a dedicated database, a fake turn parked on A, statements logged by the
server over 20 s windows (two runs, identical to within one): a held, idle turn cost **90** request
reads; with a remote watcher on B, **77** view reads per 20 s. Remote attach took **0.305 / 0.269 s**
median in two runs against **0.012 / 0.006 s** local, remote Stop **0.287 / 0.266 s** against
**0.022 / 0.009 s** (7 repetitions each). The latency is the poll: a request
waits for the holder's next read and the answer for the asker's.

## Options

1. **Keep polling, shorten the interval** — buys latency with statements, linearly.
2. **Carry the frames in the notification** — declined by D-2026-10-04 for the 8000-byte payload
   cap an `exhibit_draft` frame exceeds; still true.
3. **A wake-up in front of the same rows** — every write another process waits for also runs
   `pg_notify` in its transaction; the waiter re-reads the rows when woken and polls only as a
   fallback. Costs one dedicated `LISTEN` connection per process while it holds a turn or waits on
   one, outside the pools.

## Decision

**Option 3.** `core/notify.py` holds the one `LISTEN` connection per (loop, DSN, channel), moved out
of `science/calc/flight.py`, whose claim wake-up now uses the same class. `agent/turn_remotes.py`
notifies channel `turn_remotes`: topic `requests` on an ask or a withdrawal (every holding process
wakes and reads), the request id on an answer or a frame batch. The holder, the asker and the view
re-read on a wake-up and otherwise every `idle_poll_seconds` — a quarter of
`service_turn_relay_lease_seconds`, which keeps each lease refreshed and bounds a wake-up lost while
the listener reconnected (a reconnect also wakes every waiter). `service_turn_relay_poll_seconds`
keeps one job: how often a followed view's frames are written, so streaming is unchanged.

Measured after, same harness and runs: the held, idle turn cost **10** request reads per 20 s and
the remote watcher **8** view reads (all statements in the window: 536 → 92 idle, 787 → 92
watched); remote attach **0.032 / 0.024 s** median (local 0.008 / 0.007 s), remote Stop
**0.040 / 0.024 s** (local 0.013 / 0.011 s). `pg_stat_database` transaction rates were too
distorted by the server's lazy statistics flush to report.

## Consequences

- An idle held turn and an idle remote view cost a statement per fallback poll (2.5 s at the
  defaults), not per relay poll; a streaming view still costs one write and one read per relay poll.
- A process holding a turn, or waiting on one elsewhere, holds one more connection, on the session
  store's DSN; an idle process holds none. The chart's `fleetPools` counts pools, not these
  dedicated connections, as for the calc claim listener before.
- A watch request for a turn that ends before its holder serves it is noticed at the fallback poll
  rather than the relay poll.
- `tests/test_turn_any_replica.py::test_a_held_turn_is_read_on_a_wake_up_and_not_on_every_relay_poll`
  holds it: with a ten-second fallback, the holder and the view read at most twice in an idle
  second, and a Stop and a follow from B land within three.

Revisit when: a transaction-mode pooler is the only endpoint a deployment offers for
`session_store_dsn` (a `LISTEN` cannot work there), or `chemclaw_degraded_total{subsystem="pg_listen"}`
shows the listener reconnecting often enough that the fallback is the common path.
