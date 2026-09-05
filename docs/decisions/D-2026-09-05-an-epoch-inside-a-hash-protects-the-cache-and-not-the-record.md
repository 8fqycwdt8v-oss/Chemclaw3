# D-2026-09-05-an-epoch-inside-a-hash-protects-the-cache-and-not-the-record — exact-key lookup is safe, every browse surface was blind

## Context

`CALCULATION_EPOCH` rides inside `params_hash`. That is exactly right for the cache: `get` is
exact-key, so an epoch bump re-addresses every row and an earlier epoch's number can never be served
to a later one. The composition with `Chemclaw3-mcp`'s own epoch is real rather than asserted —
folding the server's `params_hash` through `remote_key` over a 3×3 client/server epoch grid produces
**nine distinct keys**, so a unilateral bump on either side invalidates everything.

It leaves every **browse** surface blind. `calculation_results` had no epoch column (migrations 001,
019, 024, 048), so `find_calculations` served epoch-1 rows beside epoch-2 rows for one subject with
only `computed_at` to separate them — and per the epoch log those epoch-1 rows carry wrong
linear-rotor S and G and an incomplete reactivity panel. `publish/record.py` carries `calc_version`,
`input_hash` and `params_hash`, and no epoch, so the external scientific record inherits the same
blindness.

## Decision

Migration `083` puts the epoch on the row, stamped on write with the same never-erase `CASE` as
`structure_id`, and `get`/`find` return it.

**Not backfilled.** A pre-083 row's epoch is inside an opaque digest and is unrecoverable; the empty
string means *not recorded*, and either backfill value would assert something false about some row.

`find_calculations` **serves earlier-epoch rows, marked.** `CalculationRecord.epoch_status` is
three-valued — `current` / `superseded` / `unrecorded` — because those are the three things that can
be true and calling an unrecoverable epoch `superseded` would invent a fact. Refusing them outright
was rejected: it hides real history, and an epoch bump does not necessarily wrong every number in a
payload.

## Consequences

A deployment upgrading into this release sees `unrecorded` on every row it already holds, which is
the honest answer and also a visible measure of how much of its corpus predates the column.

The published record should carry the epoch too, now that every row has one; that is a
`publish/record.py` change this ADR does not make and `BACKLOG.md` does not yet carry, because the
sink schema is a site's and widening it is a separate decision with a migration on the other side.
