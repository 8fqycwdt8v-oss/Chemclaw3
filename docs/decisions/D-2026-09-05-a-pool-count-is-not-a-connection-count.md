# D-2026-09-05-a-pool-count-is-not-a-connection-count — the fleet Postgres budget, twice corrected

**Status:** accepted · **Date:** 2026-09-05

## Context

`D-2026-08-05-the-connection-budget-is-a-fleet-number` made "keep it under the server's
`max_connections`" into a check. On 2026-09-05 that check was corrected once already: it multiplied
*processes* where a front-door process holds three pools, so it declared 16 for a process opening
48 and the shipped chart's floor was 208 against the 136 it provisioned. That commit closed with
two things written down as still open — the readiness pool being built at full width, and a split
`session_store_dsn` splitting connections across two servers. This is both, and measuring them
found the second is not a limit to document but a breach the shipped defaults already contain.

Everything below was measured on the real composition roots against a live Postgres, at the
chart's own rendered values.

## What was wrong

**A pool count is not a connection count.** `pg_fleet_pools × pg_pool_max_size` assumes every pool
is the same width and one of them is not. The `/readyz` probe borrows with its own statement
timeout — a distinct pool key, load-bearing and argued at length in `_probe_database` — and
`ops._shared_probe` collapses every concurrent caller onto one in-flight task. Driven at 1,000
concurrent `/readyz` with the cache window off: 40 checkouts, **peak one simultaneous checkout**.
A probe that blows its own budget releases before the next starts (`pool_available` back to 1
immediately after the `asyncio.wait_for` cancellation, next checkout 0.002 s).

So the fleet was charged eight connections per front-door replica for a leg that uses one. Declared
208; opens **166**. The difference was not spare headroom:

| `service.autoscaling.maxReplicas` | declared, old | real |
| --- | --- | --- |
| 6 (shipped) | 208 | **166** |
| 8 | 256 | 200 |
| **9** | **280 — `Settings` refuses** | **217** |

The front door is the only role that scales, so **raising the HPA ceiling from 6 to 9 CrashLooped
every pod in the fleet against a database that could have served it.** A guard that exists to stop
connection exhaustion was refusing a legal deployment instead.

It also *held* more than anyone thought: `pg_pool_min_size` is a floor, not a count. A `min_size=2`
pool whose first checkout races the initial fill settles at three and stays there, because
`pg_pool_max_idle_seconds` never fires on a pool something borrows from every ten seconds. Live
backends per front-door pod: 7 today, **5** with the probe's pool at one — measured in
`pg_stat_activity`, not inferred.

**A split session store breaches the shipped ceiling, and the startup check passes.** Pointing
`CHEMCLAW_SESSION_STORE_DSN` at a second database gives every pooled process one more `core/db`
pool — the front door's `/readyz` and checkpointer pools *move* there and the stores' session pool
is new. Measured per role: front door 3 → **4**, background worker 1 → 2, connector server 1 → 2.
On the shipped chart that is 320 connections against a `postgres.maxConnections` of 256, with
`Settings()` constructing happily in every pod. The only thing that notices is
`ChemclawFleetAboveItsConnectionCeiling`, ten minutes after the pods are up — which is precisely
the `mcpFace` defect `_helpers.tpl` already records, happening a second time.

**And the reason given for leaving it alone was false.** The comment said "nothing in a pod knows
the topology". Every pod is handed `CHEMCLAW_PG_FLEET_POOLS` *and* `CHEMCLAW_SERVICE_FLEET_REPLICAS`,
derived by the chart from the same autoscaling block, and `session_store_dsn` is a field on the same
`Settings` object the validator runs on. That is enough to place every pool on the server it will
actually be opened against.

## Decision

**The budget is a sum over pools of unequal width, on the server each will be opened against.**
`Settings.fleet_connections_per_server` is the one place that arithmetic lives:

- one connection per front-door replica for the `/readyz` pools, `pg_pool_max_size` for the rest;
- with no split, all of it on `postgres_dsn`'s server — 166 for the shipped chart;
- with one, `postgres_dsn` keeps one full pool per pooled process (112) and the session store
  carries what the single-DSN case carried (166);
- two DSNs naming one endpoint are one server and are summed onto it (278), so a site that split
  *databases* rather than servers is checked against the one ceiling it has. An unparseable DSN
  takes the same branch, which is the strict direction.

**`pool_max_size` is a parameter on `db.connection`, and the requested size is in the pool key.**
One caller today, which is the Rule of Three's argument against it — but it is a parameter on the
existing shared seam, not a new abstraction, and the alternatives were measured worse rather than
merely uglier (below). The size is in the key because without it the *first* caller to reach a key
decides its width for everyone landing on it afterwards, and the discriminator is a timeout
*value*, not a call site: a future borrower asking for two seconds and saying nothing about size
would inherit one connection. Measured with a second borrower holding it, `/readyz` answered 503
"database unreachable" in 2.004 s against an idle database. With the size in the key that caller
gets its own default-width pool — one extra pool, never a silently starved call site.

`min_size` is clamped under it. psycopg refuses `min_size > max_size` with a `ValueError`; raised
inside `_pool_for` that is on the request path, is not a `psycopg.Error` so `_failure_kind` returns
`None` and nothing counts or names it, and `_probe_database`'s own except clause does not catch it
either — `/readyz` would answer 500. One warm connection is also what the probe wants: 14.6 ms on a
cold pool's first checkout against 2.1 ms warm.

**`pg_session_fleet_max_connections` declares the second server, because nothing can derive it.**
The pool *count* is decidable in-process; what a second server will serve is not. Undeclared while
the split is real **warns**, for a reason unlike its sibling guard's: the shipped chart is not this
configuration and no existing release reaches the line, but an existing *split* deployment would
fail to construct `Settings()` on the `helm upgrade` that introduces the setting, over a variable
its operator has never seen. Declared with **no** split is refused — the one branch this setting
can afford to refuse, since it is new and nothing has it set. Left inert it would be a ceiling for
a server that does not exist, which the alert *adds* to the real one, so a fleet could sit above its
actual limit with nothing firing.

## What was measured and rejected

**A dedicated setting read inside `_pool_for`.** That function sees only `(dsn, options)`, so to
know it is building "the readiness pool" it would match the options string against the front door's
readiness timeout. The options string is a legitimate *identity* discriminator — it already is the
key — and not a legitimate *policy* one, which it encodes only by coincidence of a number. Measured
by simulating exactly that discriminator across the pool, chart and config suites: it silently
reshaped `tests/test_db_pool.py::test_the_reported_per_process_ceiling_counts_pools_and_not_processes`,
whose hand-built "/readyz shape" asks for the same timeout and no size (9 != 12). A production call
site would be reshaped the same way, silently. Under the chosen shape that test passes untouched.

**`/readyz` on a dedicated `db.connect()`.** It removes a pool outright and is the worst arm
measured. Steady state costs +11.5 ms per probe (12.8 ms median against 1.2 ms pooled), which in
aggregate is nothing — 36 probes a minute fleet-wide. The cost is the tail, and it reproduces
`core/db`'s own recorded finding exactly:

| event-loop pressure | warm pooled checkout | fresh `db.connect()` |
| --- | --- | --- |
| none | 1.6 ms, 0/15 over the 2 s budget | 12.1 ms, 0/15 over |
| 8 hogs × 10 ms | 1.29 s, **0/8 over** | 3.06 s, **8/8 over** |
| 64 hogs × 10 ms | 7.70 s | **all fail** after 12.8 s |

At moderate pressure it turns a healthy pod into a 503 on every probe; at high pressure it exceeds
the kubelet's `timeoutSeconds: 5` by more than twice and the pod is drained for being busy. That is
the readiness amplification the separate pool exists to prevent, arriving through a different door,
and `connect()` is the one path with no warm connection at all — so it re-pays a handshake under
exactly the condition that makes handshakes fail.

**A `postgres.splitSessionStore` boolean in the chart.** The chart cannot validate it against
reality at all: `secrets.create` defaults to false, no template reads a Secret's contents, and even
at `create: true` the rendered Secret has no `CHEMCLAW_SESSION_STORE_DSN` key. It would be a pure
operator assertion that can silently disagree with the process that already knows the truth — the
`map_to_hpc_identity` shape this tree deletes.

**Leaving it documented, as the previous commit did.** The shipped defaults with a split open 320
against their own declared 256, and the "documentation" was one code comment: no `BACKLOG.md` row,
no `DEFERRED.md` row, no ADR. Documenting a limit the shipped defaults already breach is not
documenting a limit.

## Consequences

The declared floor falls 208 → **166** and `postgres.maxConnections` stays at 256, which is now
90 connections of headroom rather than 48 — and `maxReplicas: 9` renders and starts. Live backends
per front-door pod fall 7 → 5. **No existing release changes behaviour**: with no split not a single
number moves, and the refusal that was removed only ever fired on deployments the fleet could serve.

What a split deployment gains is a warning naming the connections nobody was checking, and a second
ceiling to declare. What it does not gain is silence: `chemclaw_pg_session_fleet_max_connections`
joins the alert's comparison, so the runtime half covers both servers where it previously summed
them against one.

`tests/test_fleet_pools.py` carries the measurements the arithmetic stands on — the probe's pool is
one connection on both ends, its peak concurrency is one, and a split adds exactly one pool per
pooled process — and both mutants were checked: removing `pool_max_size=1` fails two of them,
removing the single-flight fails the concurrency one and nothing else.

## What this does not do

`chemclaw_pg_pool_max_size` still carries no DSN label, so the runtime alert sums both servers'
pools against both servers' ceilings rather than checking each. That is conservative in the
direction that pages rather than misses, and the startup check is the honest half for a split. A
labelled gauge is a `BACKLOG.md` row, not a decision taken here.
