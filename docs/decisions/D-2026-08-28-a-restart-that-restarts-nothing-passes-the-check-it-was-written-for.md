# D-2026-08-28-a-restart-that-restarts-nothing-passes-the-check-it-was-written-for — the lane's Postgres verbs had no compose branch, and the chaos check could not tell

## Status

Accepted, 2026-08-28.

## Context

`infra/live/bootstrap.sh` brings the live lane's Postgres and Temporal up two ways: through
`infra/docker-compose.yml` when a Docker daemon is reachable, and natively otherwise. It also
exposes four verbs the storm's chaos family drives — `restart-postgres`, `stop-temporal`,
`start-temporal` — so that, in its own words, "one place knows how this stack is started, so a
chaos test cannot restart it differently from how it was brought up and then measure the
difference."

Those verbs were written against the native path. An earlier pass noticed that
`stop-temporal`/`start-temporal` therefore did nothing on a Docker lane and fixed them, adding
`compose_temporal_id()` and a compose branch to each. **The identical defect was still live for
Postgres and nobody had looked**, because the Temporal fix was written as a fix to Temporal rather
than as a fix to a class.

## The measurement

On the Docker lane — the only lane this environment can run — `bash infra/live/bootstrap.sh
restart-postgres`:

```
[live] postgres not running
[live] postgres already accepting connections on 5432
[live] postgres up on 5432 (pgvector 0.8.6)
```

and `select pg_postmaster_start_time()` before and after:

```
2026-08-28 15:35:59.634885+00:00
2026-08-28 15:35:59.634885+00:00
```

Byte-identical. `stop_postgres` guards on `[ -f "$PGDATA/postmaster.pid" ]`, and on a compose lane
`$PGDATA` does not exist, so it returned having done nothing while reporting "postgres not
running" about a database that was serving. `start_postgres` then found `pg_isready` already true
and reported "postgres up". Three log lines, all true as sentences, describing a restart that did
not occur.

`cli/live_storm.py::_chaos_postgres_bounce` drives that verb. Its last recorded run scored it
**PASS**, with `24/24 in-flight turns survived the bounce; a fresh turn answered 2.1s after it`.
Both halves of that observation are what a run doing nothing scores.

## Decision

**Two changes, and the second is the one that matters.**

1. `compose_temporal_id()` is generalised to `compose_service_id <name>`, and `start_postgres` /
   `stop_postgres` get the compose branch `start_temporal` / `stop_temporal` already had. The
   readiness budget is a named, overridable constant (`CHEMCLAW_LIVE_PG_READY_TIMEOUT`, 90s, the
   same budget the Temporal branch uses) rather than a literal in the loop, and the failure tails
   the container's own log. Measured after the change: `pg_postmaster_start_time()` moves.

2. **`_chaos_postgres_bounce` now asserts that the bounce happened.** It reads the postmaster's own
   start time before and after, on a connection outside the pool under test, and a run where the
   time did not move is a **failure of this check** rather than a pass. Fixing the primitive is not
   a reason to keep trusting the check that could not see the primitive break — the next lane
   primitive to silently no-op will be a different one, and this is the only assertion that
   notices.

## Consequences

- The E-family database-bounce result from every run before today says nothing about pool recovery
  on a Docker lane. It is not evidence and should not be cited as any.
- A chaos check that disturbs something must observe the disturbance, not only the recovery. The
  rule generalises to the other three verbs; `_chaos_worker_kill` already reads the workflow's
  state at kill time, and `_chaos_broker_outage` fails loudly when its own `start-temporal` does
  not come back, so those two were already honest. `restart-postgres` was the one that was not.
- `tasks/lessons.md` carries the rule: a lane primitive that branches on Docker must branch in
  every verb, and a check that uses one must prove the disturbance happened.
