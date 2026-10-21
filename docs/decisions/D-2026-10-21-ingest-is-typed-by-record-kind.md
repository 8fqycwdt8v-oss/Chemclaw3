# D-2026-10-21-ingest-is-typed-by-record-kind — an ingest half declares the kind of record it produces, and each kind owns its model, store and query

**Status:** proposed · **Date:** 2026-10-21

## Context

The ingest half is `ElnAdapter.map_to_ord(raw) -> OrdReaction`, and `reaction_records` is written
only through `record_from_ord_reaction`. Data that is not a reaction has needed new machinery
every time:
- **Commitments** became a third half type. It touched about 25 files across `ingest/`, `durable/`,
  `agent/`, `core/config/`, `api/` and a migration.
- **Documents** needed their own durable sync.

The next sources a process-R&D site attaches are LIMS results, HTE plates, stability studies and
batch records. Under the current seam each would be another half and another core change.
`analytical/` (acceptance criteria and verdicts) has no ingested input at all.

## Options

1. **One half per new shape** (status quo). Every new shape is a core change in about six packages.
2. **Record kinds.** `ingest:` declares `{adapter, kind}`. A `RecordKind` owns its pydantic model,
   store, evidence projection and query columns. The ingest half becomes `fetch_new_entries` plus
   `map(raw) -> K`, and one durable `record_sync` serves every `(source, kind)`.
3. **A generic JSON record table.** Any shape ingests with no code, nothing is typed or queryable,
   and the provenance and retraction rules have nothing to hold on to.

## Decision

**Option 2.**
- `reaction` wraps the current path byte for byte. `ElnAdapter.map_to_ord` is that kind's `map`.
- `commitment` and `document` migrate onto the seam, deleting the third-half special cases.
- `measurement` and `plate` are new. `measurement` feeds `analytical/`. Each `plate` well also
  yields a `reaction` row.
- The warehouse `IngestBinding` derives its mappable fields from the kind's model, so a LIMS view
  attaches by manifest.

## Consequences

- A bare `ingest:` string means `kind: reaction` for one release, then it is refused.
- `durable/record_sync.py` replaces `eln_sync` and `commitment_sync`. Their workflow types stay
  registered for one release, with replay tests.
- New tables (`analytical_results`, plate tables) are created with `tenant_id` and RLS.
- Held by a test that adds a fixture kind and counts the files it touched: one module, one
  migration, one manifest.
- Plan item: W11. Depends on W4 and W7.5.
