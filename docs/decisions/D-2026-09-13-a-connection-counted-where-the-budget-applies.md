# D-2026-09-13-a-connection-counted-where-the-budget-applies — a held connection is one backend, on the endpoint it dials

`ChemclawFleetAboveItsConnectionCeiling` compares `sum(chemclaw_pg_pool_max_size)` against the
declared per-server ceilings, and `core/db._process_max_connections` is what produces the left-hand
side. It sums `pool.max_size` over every pool the process holds, built here or registered as foreign.

`publish/drivers/postgres.py` holds something that is not a pool.

## What was measured

`PostgresWarehouse._connection` opens a bare `psycopg.AsyncConnection`, autocommit, and keeps it for
the driver's life. It is in neither `_POOLS` nor `_FOREIGN_POOLS`, so a worker holding one reported a
ceiling one lower than the process could reach — per enabled sink, on the gauge an alert reads.

**The `BACKLOG.md` row's own proposed fix raises.** It said "register it the way
`agent/checkpointer.py` registers its foreign pool"; `_process_max_connections` sums `pool.max_size`,
and measured:

    AttributeError: 'AsyncConnection' object has no attribute 'max_size'

**Two scope corrections to that row, both of which change what the right answer is.** Its title says
"a result sink *on the primary server*", which reads as the front door: the drain runs on
`settings.background_task_queue`, so the process holding the connection is a **worker**, and "on the
primary server" is about the sink's *target*. And that target is, by design, a database this system
does not own (`D-2026-08-25-a-cache-is-not-a-record`) — so a blanket count would charge a foreign
warehouse's backend to `postgres.maxConnections`, which is a ceiling on this deployment's server.

## The decision

**Count connections, not a connection dressed as a pool, and count them on the endpoint they dial.**

`core/db.register_connection(conn, conninfo)` records a dedicated connection worth exactly one
backend; `unregister_connection` releases it. `_process_max_connections` adds the registered
connections whose `pg_endpoint(conninfo)` matches `postgres_dsn`'s, and
`_session_store_max_connections` adds the ones matching the split store's — the same arithmetic, and
the same comparison, the pools already go through, so whatever `pg_endpoint` cannot tell apart neither
half tells apart.

A `max_size = 1` shim was the other candidate and is declined: `pool_stats` walks `_all_pools()` and
calls `get_stats()` on each, so a shim would have to impersonate a pool's whole surface to stay
counted, and the next reader would find an object claiming to be a pool that cannot be borrowed from.
A connection is not a pool; the registry now has a word for each.

**A sink on its own warehouse counts zero, and `deploy/README.md` says whose arithmetic it is.** That
is the row's second option taken *beside* the first rather than instead of it: register it, and state
in the operator documentation that a sink elsewhere is one connection per drain-running worker replica
per enabled sink on that server, held for the pass, against a ceiling this chart does not declare.

**The release in `aclose` stays, and is asserted.** `_held_connections_on` drops a closed connection
on every read, so the *count* is correct without it — but the drain builds a new driver every pass
(deliberately, so a rotated credential takes effect on the next run), and without the release the
registry grows by one dead entry per pass until somebody reads the gauge. A mutation removing the
release initially **survived**, which is how that assertion came to exist: the test now counts
open/close cycles with no read in between.

## What keeps it true

| property | test |
| --- | --- |
| a sink on `postgres_dsn`'s own server raises what the process reports it may open, by one | `tests/test_publish_sink_bounds.py::test_a_sinks_held_connection_is_counted_where_the_budget_applies` |
| a sink on a warehouse of its own raises it by **nothing** — the arm a blanket count fails | same test |
| the count falls again when the driver closes | same test |
| four open/close cycles leave no dead entries in the registry, with no gauge read in between | same test |
| the foreign-pool registration the checkpointer uses is unchanged | `core/db.register_pool` and its own tests |

Read through `_process_max_connections` rather than off `_HELD_CONNECTIONS`: the registry is the
mechanism and the sum is what the alert reads.

Three mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| the driver stops registering its connection (the pre-fix state) | red |
| `_held_connections_on` counts every registered connection regardless of endpoint | red on the named arm — *"a sink pointed at a warehouse of its own was charged to the primary server's budget: 1 against 0"* |
| the driver stops releasing on `aclose` | **survived** the first version of the test, and is red against the version that counts registry growth — recorded because the first result is the finding |
