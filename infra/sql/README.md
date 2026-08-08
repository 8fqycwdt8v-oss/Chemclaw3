# `infra/sql/` — the schema

**Responsibility:** every table this system has, in ordered `.sql` files applied by
`chemclaw.core.migrate` against a `schema_migrations` ledger with per-file checksums (D-034). The
schema is **forward-only and additive** — no migration may drop, rename, truncate or delete
(D-2026-08-04-the-schema-only-goes-forward, enforced per file by
`tests/test_migrations_are_additive.py`). New SQL is a new numbered file; an applied file is never
edited, because the ledger flags the changed checksum as drift.

`grants/` is not part of that set and is invisible to the runner's non-recursive glob by
construction. See the note at the bottom.

## The tables

One row per table. **Written by** names the module that owns its writes — the store, not every
caller. **Disposal** is what bounds its growth, and a blank there means nothing does; the
`docs/planning/BACKLOG.md` row for the tables retention neither prunes nor refuses is the record of
which those are.

`tests/test_schema_inventory.py` checks this table against the `CREATE TABLE` statements on disk in
both directions, because an inventory nobody verifies is read, believed, and wrong — the only other
table inventory in this repository sits in `docs/archive/` and is seventeen migrations stale.

| Table | Migration | Written by | Disposal |
| --- | --- | --- | --- |
| `schema_migrations` | 000 | `core/migrate.py` | never — the ledger is the record of its own work, and the runtime role cannot write it at all |
| `calculation_results` | 001 (+024 indexes) | `science/calc/postgres_store.py` | **refused**: evicting a cached result silently converts a hit into a recomputation, potentially an HPC run (D-011). Bounded by cost policy, not by a clock |
| `molecule_fingerprints` | 002 (+004) | `science/fingerprints/store.py` | — |
| `reaction_fingerprints` | 003 (+004) | `science/fingerprints/store.py` | — |
| `audit_events` | 006 (+010, 011, 026) | `agent/audit_store.py` | **refused**: deleting from a hash chain is indistinguishable from the tampering it detects. Safe disposal needs archive-then-reseal — BACKLOG STO-13 |
| `sync_cursors` | 007 | `ingest/eln/cursor.py` | — (one row per ingest source; bounded by the source count) |
| `session_messages` | 008 (+022) | `agent/session_store.py` | `durable/retention.py`, per session through the pairing closure (D-145), plus in-line compaction on write (D-151) |
| `session_events` | 009 (+014, 028) | `agent/session_events.py` | `durable/retention.py`, **consumed rows only** — an undelivered push-back must outlive the window that would have destroyed it |
| `note_index` | 012 (+035, 039) | `retrieval/vector_index.py` | derived and rebuildable (`make reindex`, which now also heals a model change); rows for deleted notes are not removed |
| `session_owners` | 013 (+021) | `agent/session_store.py` | — (survives its session's pruned history; BACKLOG) |
| `user_preferences` | 015 | `agent/preferences.py` | — |
| `predictions` | 016 | `science/calc/calibration.py` | — |
| `subscriptions` | 017 (+029) | `agent/subscriptions.py` | deleted on unsubscribe |
| `session_turns` | 018 | `agent/session_store.py` | released at turn end; a leased row, so an abandoned claim expires |
| `artifact_blobs` | 019 | `science/calc/postgres_artifacts.py` | `durable/artifact_eviction.py`, by idle window and size budget (both off by default) |
| `calculation_artifacts` | 019 | `science/calc/postgres_artifacts.py` | cascades from `artifact_blobs` |
| `plan_approvals` | 020 (+034) | `agent/plan_approval_store.py` | — (consumed rows are marked, not removed) |
| `job_records` | 023 (+033) | `durable/job_record_store.py` | **refused**: the table exists because a durable run's result used to expire with Temporal's history and take a campaign's evaluation record with it (D-157) |
| `observations` | 025 | `memory/observations.py` | stale rows retired by status, not deleted |
| `note_proposals` | 027 | `kg/proposal_store.py` | — |
| `measurements` | 030 | `science/calc/calibration.py` | — |
| `bo_campaigns` | 031 | `science/bo/campaign_record_store.py` | — |
| `bo_suggestions` | 031 | `science/bo/campaign_record_store.py` | cascades from `bo_campaigns` |
| `audit_anchors` | 032 | `agent/audit_anchor.py` | never — an anchor is the evidence a trailing truncation happened, so the runtime role cannot delete one |
| `turn_costs` | 033 | `agent/turn_cost_store.py` | — |
| `document_files` | 037 (+040, 041) | `ingest/documents/index.py` | `ingest/documents/sync.py`, mark-and-sweep: rows a *complete* crawl did not see are removed, so a file deleted from the share leaves the index. Never swept on an incomplete crawl — an unmounted share and an empty one look identical |
| `document_chunks` | 037 (+038, 040, 041) | `ingest/documents/index.py` | cascades in effect from `document_files`: the same sweep deletes any *cutting* — `(doc_id, chunking_key)` — no remaining file row claims, and `upsert` applies the identical predicate to the documents it writes. Derived and rebuildable — dropping both tables and re-running the sync reconstructs them |

## Two things the shape of this table will not tell you

**There are two foreign keys in the whole schema** (`calculation_artifacts` → `artifact_blobs`,
`bo_suggestions` → `bo_campaigns`), both where a cascade is load-bearing. Everything else is
associated by a shared id with no constraint — including the four `session_*` tables, which is why
pruning one of them does not touch the others and why the **Disposal** column has to be read per
row rather than per subsystem.

**`grants/` is applied by `make db-grants`, not by `make db-migrate`.** The migration set runs each
file exactly once, tracked by checksum, which is right for a schema change and wrong for a grant: a
grant is a reconciliation between a schema that keeps growing and a runtime role that may be created
at any point, so run-once semantics would leave every later table ungranted and break the
application on first use of it. It re-runs on every deploy, after the migrations, and no-ops where
no `chemclaw_app` role exists (D-2026-08-05-append-only-by-grant-not-by-contract).
