# D-2026-10-21-a-source-is-queried-through-typed-generated-tools — structured questions go to tools generated from a declared `query:` binding, never to model-written SQL

**Status:** proposed · **Date:** 2026-10-21

## Context

At question time a source is reached only through `SourceRetriever.retrieve(query: str, filters)`,
where the filters are `type`, `tag`, `since` and `until`. `gather_evidence` sends one string to
every source and fuses the ranks. Once ingested, ELN rows in `reaction_records` are reachable only
by that sweep, by DRFP similarity and by `expand_note`.

"Every Suzuki coupling with yield above 80 % in 2-MeTHF since 2024" therefore has no tool, though
the warehouse binding (D-2026-08-26-the-driver-s-signature-is-the-schema) already knows every
column. `ingest/eln/warehouse/sql.py` builds only fixed statements, with values always bound.

## Options

1. **Text-to-SQL.** The model writes SQL against a described schema. It is the most expressive,
   and it gives a model-authored statement a database connection. Correctness, injection and row
   cost all become prompt properties.
2. **Typed tools generated from a declared `query:` block.** The binding lists queryable columns,
   their types, units and allowed operators. A tool is generated whose parameters are those
   filters. SQL is built only from declared identifiers, and values are always bound. This is less
   expressive, and every query is one the operator allowed.
3. **Keep only retrieval.** Structured questions get approximate answers from a text sweep, and
   absences cannot be told from misses.

## Decision

**Option 2.**
- `WarehouseBinding` gains `query:` (`QueryBinding`, `QueryColumn`).
- `sql.query_statement` builds `WHERE` from declared columns and operators only, ends `ORDER BY`
  on the key, and fetches `max_rows + 1` to report truncation. `max_rows` is at most 200.
- Each row carries a citation naming its source (D-2026-09-13).
- The same generated shape serves core's own `reaction_records` as `query_reaction_records`, so
  data already ingested becomes filterable. Structure filters reuse `molfp` and `rxnfp` rather than
  SQL.
- `gather_evidence` gains `sources:` and reports the structured tools covering the sources that
  answered.

## Consequences

- No statement text ever comes from the model. `tests/test_warehouse_binding.py` gains exact
  statements per dialect, and every value position is fuzzed with injection payloads.
- Dialect differences (`ILIKE`, date literals) move onto the `Warehouse` driver beside
  `vector_dialect`.
- New indexes on `reaction_records` JSONB paths are created with `tenant_id` and RLS.
- Measured by the source eval of plan item W9.3: at least 25 points over the
  `gather_evidence`-only baseline, judged by a model other than the one under test.

Revisit when: a query the source eval needs cannot be expressed as declared filters (joins across
relations, aggregates). The next step is declared views, not model-written SQL.
