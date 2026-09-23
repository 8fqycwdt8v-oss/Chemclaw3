# D-2026-09-23-the-server-says-which-server-it-is — measuring a Postgres server's identity

**Status:** accepted · **Date:** 2026-09-23 · Amends
`D-2026-09-05-a-pool-count-is-not-a-connection-count`, whose own "what this does not do" says a
measured cluster identity is a row and then did not write one. Closes the `BACKLOG.md` row
*"Neither net sees one Postgres server that two DSNs spell differently"*.

## Context

`core/config.pg_endpoint` compares strings. Both halves of the connection budget split the fleet
with it — `Settings.fleet_connections_per_server` at startup and `db._session_store_max_connections`
for the runtime gauge — so `localhost` against `127.0.0.1` is **one server charged to two
ceilings**, with the real total checked by nothing.

That is a regression rather than a gap: the released expression before the split gauge existed
compared one sum against one ceiling and would have caught it.

Driven against the live server before anything was written:

```
primary spelling  ('localhost', '5432')
session spelling  ('127.0.0.1', '5432')
pg_endpoint says one server?  False
system_identifier, both       7687905078163619878
process max connections       32
charged to the "other" server 32   <- all of it, to a server that does not exist
```

## Decision

**Ask the server, on a borrow that has already succeeded, and cache the answer.**

- **`system_identifier` is the right probe and `inet_server_addr()` is not.** The first is assigned
  once at `initdb` and never changes for the life of a server; the second is the DSN's own spelling
  laundered through the kernel — measured previously, one server answers `NULL` over a socket,
  `127.0.0.1` over loopback and its bridge address over the bridge.

- **Readable without privilege, which is what makes it shippable.** Driven against a freshly
  created `NOSUPERUSER NOCREATEDB NOCREATEROLE` role: it reads. 1.5 ms on the loopback server
  `make up` runs.

- **The trade the row framed as "the decision" is not forced.** The row, and the gauge's own
  docstring, refused a measured identity because it "would be unknown until a pool filled, so the
  fleet-ceiling alert would lose its series during a database outage". That is true of an identity
  read *at scrape time* and false of one **cached**: `system_identifier` cannot change while the
  server is the same server, so one borrow is enough for the life of the process. Before the first
  borrow `same_server` falls back to the string comparison, which is today's behaviour exactly;
  after it, the cached value answers, including through an outage. Neither branch is ever worse
  than what shipped, so the alert keeps its series and there is nothing to trade.

- **Learned on the borrow, never in the gauge.** A Prometheus gauge source is synchronous and a
  scrape must not make a network call — the rule `jobs_in_flight_refresh_seconds` states one
  subject over. So the read happens in `core/db.connection()`, costing one round trip on the
  *first* borrow against an endpoint and nothing afterwards. It cannot happen at pool construction
  either: there is no connection yet.

- **A split the measurement disproves is collapsed to zero, and finding that took the
  measurement.** The first version of this change taught `same_server` to answer correctly and
  stopped — which made the gauge *worse*: `fleet_connections_per_server` had already decided from
  the two DSN strings that there were two servers, so with the pools now all matching "the session
  server", **32 of 32** connections were carved out to a box that does not exist. The honest
  carve-out for a disproved split is zero, which restores the single summed expression the split
  gauge regressed.

- **An unreadable identity is attempted once.** A role without the grant, or a fork with no
  `pg_control_system()`, would otherwise pay a failing query on every checkout for the life of the
  process. One attempt, one warning naming the consequence, then the string comparison for good.

- **`pg_endpoint` is unchanged and still compares strings**, for every reason its own docstring
  gives: `Settings()` runs at module import with no event loop and no pool, and a validator that
  dialled would make every unit test and every `make *-validate` a network call and every database
  outage a configuration error in every process.

## Consequences

**The two halves can now disagree, deliberately and in one direction.** `pg_endpoint`'s docstring
said they could not, which was the point of the gauge reusing it; that sentence now carries the
exception. The startup check charges a phantom split to two ceilings — it has no database to ask —
and the runtime half collapses it once a borrow has disproved it. The startup check is the
conservative one, so the disagreement is safe in the direction it happens.

**Both directions are driven against real servers**, not simulated: a second Postgres on another
port answers a different `system_identifier`, and a genuine split keeps carving 16 of 32 out to it
while the phantom split collapses to 0.

**Revisit when:** somebody wants the *startup* check to agree. That needs a probe that can run
before a loop exists, and the file that would show this decision is wrong is
`tests/test_fleet_pools.py`, whose fallback test fails the day `same_server` stops matching
`pg_endpoint` on an unmeasured pair.
