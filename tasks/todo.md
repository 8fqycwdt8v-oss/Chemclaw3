# Publish every computed value as a queryable scientific record

The investigation asked how computed values are stored — for reactions, single and multi-compound
calculations, and conformer ensembles — and whether *any* calculation could be stored in a highly
structured database outside this system. This is the implementation.
`docs/decisions/D-2026-08-25-a-cache-is-not-a-record.md` is the decision; this is the working record.

## What the investigation found (read this before the list)

Five stores hold computed values. **None is a scientific record.** `calculation_results` is a cache
— `key` onto an opaque `result JSONB` — and its own query model refuses any predicate on the
payload, deliberately. That is right for exact-key lookup, and it is exactly why the cache cannot
also be the record.

Two findings shaped everything downstream:

- **Composites are not persisted at all.** After `D-2026-08-16` a composite whose key would name an
  output is decomposed rather than cached, so `compute_thermochemistry`, `compute_reaction_energy`,
  `compare_solvents` and the weighted ensemble exist only as `job_records.result` JSONB — and on the
  conversational path, nowhere beyond a TTL-swept trace blob. The shapes a chemist reasons about
  were the least recorded.
- **Nothing published a computed value outward.** Every outbound HTTP client in the tree is a fetch.

## Done

- [x] **Canonical record** — `publish/record.py`. One subject shape for all five cases; identity
      excludes solvent/temperature/method so cross-solvent comparison is a `GROUP BY`.
- [x] **Property registry** — `publish/properties.py`, 78 properties. The FK that keeps the fact
      layer from being EAV: a value cannot be written under a name nobody defined.
- [x] **Solvent canonicalization** — `publish/solvents.py`. 42 upstream names → 25 solvents.
- [x] **Projection** — `publish/project.py`, 17 result shapes.
- [x] **Shipped schema** — `schema/result-store/001_core.sql`, 21 tables, portable (no arrays, no
      sequences, no partial/expression indexes). `make sink-schema` prints it plus a generated seed.
- [x] **The sink seam** — `sink.yaml`, registry, two drivers (SQL + HTTP) and a psycopg `Warehouse`.
- [x] **Outbox + drain** — migration 050, `PublishResultsWorkflow`, three enqueue hooks, retention,
      grants, metrics.
- [x] **CLI + bundle** — `sink_schema`, `backfill_publications`, `validate_sinks`, and the
      `results` jobs-only bundle for the deliberate republish.
- [x] **Tests** — the six chemistry questions as SQL against a live database, projection
      round-trip with a field-coverage check, registry coherence, solvent parity, outbox durability.

## Three defects the tests found that reading the code had not

Worth keeping, because each was silent and each is the kind that recurs.

1. **Species were matched to members by list position.** `species` and the equation's own
   `reactants`/`products` are independently produced sequences — a `quick` run returns no species at
   all. A two-species breakdown over a three-member equation put cyclohexane's free energy on
   butadiene. Both are plausible numbers in the same units. Now matched on `(role, molecule)`.
2. **The two ensemble shapes carry different halves and neither carries both.** `EnsembleMember`
   has an absolute energy and no population; `Conformer` has a population and no absolute energy.
   Requiring either would have made half the ensembles unpublishable.
3. **A field-coverage check found three real gaps** reading the models had missed: a missing
   descriptor, an exotherm flag published without the threshold it was judged against, and a scan
   whose coordinate said "dihedral" without saying which atoms.

## Verified

- `make lint`, `make type` green. Full suite run against a live Postgres/Temporal
  (`sudo dockerd; make up; make db-migrate`) — see the review below for what remains red and why.
- **End to end, not just unit**: a calculation through the real `cached_compute` path → outbox →
  `drain_result_publications` → a second Postgres running the shipped DDL, landing as typed rows
  with unit, uncertainty, method and molecule. Redelivery verified as a no-op.
- The generated DDL + seed load into a clean schema: 21 tables, 78 properties, 25 solvents, 42
  aliases, and `tetrahydrofuran` resolving to `thf`.
- `backfill_publications` queues on the first run and writes nothing on the second.

## Review

**What went well.** The plan's acceptance check — the six questions as SQL — was the right bar: it
caught the species-matching corruption, which no amount of re-reading the projector would have. The
field-coverage test (a payload that records which keys were read) is worth reusing anywhere a model
is projected into another shape.

**What I would do differently.** I reused the warehouse seam's `ConnectionBinding` for the SQL
sink's connection block before noticing it is Snowflake-shaped and has no host or port. The fix —
letting the driver's own signature be the schema — is what the data-source seam already does for
`config:`, and I should have started there.

**Left deliberately open**, both queued in `BACKLOG.md` §4: no deployment yet points at a real
results database, and nothing has measured rows-per-calculation on a real corpus. That second one
also decides whether `property_value` needs partitioning, which is why no partition key is chosen —
picking one before the row count is known would be a guess.
