# `schema/` — the schemas ChemClaw3 ships for databases it does not own

Everything under `infra/sql/` is *this* system's own database, applied by `make db-migrate`.
Everything here is a schema for a store **somebody else runs**, published so a DBA can apply it.

The distinction is the reason the two are not in one directory: this system never holds DDL
privileges on the databases here, and its runtime principal is deliberately not the principal that
can define their tables — the same split `postgres_migration_dsn` and `postgres_dsn` already make
one level in.

## `result-store/`

The canonical schema for a computed-results database: every calculation this system performs, as a
queryable scientific record. `src/chemclaw/publish/README.md` explains what is written and why;
this is the shape it is written into.

```
python -m chemclaw.cli.sink_schema           # the DDL alone
python -m chemclaw.cli.sink_schema --seed    # the registry rows it needs to be usable
python -m chemclaw.cli.sink_schema --all     # both, in the order they must be applied
```

**The seed is generated, not stored.** It comes from `chemclaw.publish.properties` and
`chemclaw.publish.solvents`, which is also what the writer canonicalizes against — a checked-in
seed file that drifted from them would build a database whose foreign keys reject rows this system
considers valid.

### How it is versioned

Forward-only and additive, under the same rule `tests/test_migrations_are_additive.py` enforces for
`infra/sql/`: a new column arrives nullable with a default, so an older ChemClaw keeps writing
unchanged and a newer one reads the absence as "not recorded".

Three tiers, in order of how often each applies:

1. **A new property is no DDL at all** — it is a row in `property_definition`, and the publisher
   upserts the shipped registry on startup. This is the majority case, and the whole point of the
   registry: a new calculator ships rows, not migrations.
2. **A new column** is `ADD COLUMN IF NOT EXISTS … NOT NULL DEFAULT ''`.
3. **A new table** is `CREATE TABLE IF NOT EXISTS`, unreferenced by the older image.

The publisher **writes down to the schema it finds**: it probes `information_schema.columns` once
and omits columns the site lacks rather than failing every row, because a schema *lag* turning into
a total publish outage is the worse failure.

### Portability

Written to load on PostgreSQL, Snowflake and Oracle. Three Postgres habits are given up for that,
each deliberately:

- **No arrays.** Lineage is an edge table and warnings are rows. Neither Snowflake nor Oracle has
  `TEXT[]`, and an array cannot be indexed in the reverse direction staleness propagation walks.
- **No sequences.** Every primary key is a content hash — which also makes re-publishing a record
  a no-op rather than a duplicate, and the whole database re-buildable from `calculation_payload`.
- **No partial or expression indexes.** Anything worth indexing is a real column.
