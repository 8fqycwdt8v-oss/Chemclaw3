# `infra/sql/` — the schema

**Responsibility:** every table this system has, in ordered `.sql` files applied by
`chemclaw.core.migrate` against a `schema_migrations` ledger with per-file checksums (D-034). The
schema is **forward-only and additive** — no migration may drop, rename, truncate or delete
(D-2026-08-04-the-schema-only-goes-forward, enforced per file by
`tests/test_migrations_are_additive.py`). New SQL is a new numbered file; an applied file is never
edited, because the ledger flags the changed checksum as drift.

That check asks **two** questions, because destroying data and ending the rollback are different
things with different answers (D-2026-08-08-a-rollback-that-is-not-a-schema-step). Destroying data
is refused outright. Leaving the *previous image* unable to write — `SET NOT NULL` on an existing
column, or a dropped or replaced key — is refused unless the migration is listed in
`_REVIEWED_ROLLBACK_BREAKS` with the statements read and an ADR saying what an operator does
instead of "deploy the previous image". Exactly one migration is: `041_document_chunk_identity.sql`,
whose rollback procedure is in that ADR.

`grants/` is not part of that set and is invisible to the runner's non-recursive glob by
construction. See the note at the bottom.

## The tables

One row per table. **Written by** names the module that owns its writes — the store, not every
caller. **Disposal** is what bounds its growth, and a blank there means nothing does; the
`docs/planning/BACKLOG.md` row for the tables retention neither prunes nor refuses is the record of
which those are.

`tests/test_schema_inventory.py` checks this table against the SQL on disk, because an inventory
nobody verifies is read, believed, and wrong — the only other table inventory in this repository
sits in `docs/archive/` and is seventeen migrations stale. It checks the **set** of tables in both
directions, and the **Migration** column against the statements that name each table. That second
check is newer than this paragraph, and it was added because the column was itself the example:
four of twenty-seven rows named only the migration that created the table and omitted a later one
that added a column to it. **Written by** and **Disposal** stay unchecked on purpose — they are
judgements, and a test for them would be a second copy of the answer.

A cell lists **every** migration that touches the table, oldest first, so a row answers "when did
this last change shape". Two files may share a number — `037` is both `037_bo_suggestion_provenance.sql`
and `037_document_index.sql` — and the cell says `037` once; the ledger tracks whole filenames, so
the pair applies in filename order and neither shadows the other.

| Table | Migration | Written by | Disposal |
| --- | --- | --- | --- |
| `schema_migrations` | 000 | `core/migrate.py` | never — the ledger is the record of its own work, and the runtime role cannot write it at all |
| `calculation_results` | 001 (+019 `compute_seconds`, 024 indexes, 048 `structure_id`) | `science/calc/postgres_store.py` | **refused**: evicting a cached result silently converts a hit into a recomputation, potentially an hours-long CREST search (D-011). Bounded by cost policy, not by a clock |
| `molecule_fingerprints` | 002 (+004, 046 index) | `science/fingerprints/store.py` | — |
| `reaction_fingerprints` | 003 (+004, 046 index) | `science/fingerprints/store.py` | — |
| `reaction_labels` | 051 | `science/labels/store.py` | derived and rebuildable: drop it and re-run the corpus drain plus the label backfill |
| `reaction_species` | 051 | `science/labels/store.py` | derived and rebuildable; a species the source amended away is deleted with its reaction's record phase |
| `corpus_molecules` | 054 | `ingest/labels/corpus.py` | derived and rebuildable: refilled by re-draining the corpus |
| `audit_events` | 006 (+010, 011, 026, 044, 045) | `agent/audit_store.py` | **refused**: the trail is the record of who ran what, and disposing of it is a policy decision for whoever owns that record rather than an age cutoff in a cleanup job. `prev_hash`/`row_hash`/`chain_version` are retired columns, unwritten, at their defaults |
| `sync_cursors` | 007 | `ingest/eln/cursor.py` | — (one row per ingest source; bounded by the source count) |
| `session_messages` | 008 (+022, 026, 043, 046 `message_shape` check) | `agent/session_store.py` | `durable/retention.py`, per session through the pairing closure (D-145). The in-line compaction on write this row used to name went with the engine that needed it |
| `session_events` | 009 (+014, 028) | `agent/session_events.py` | `durable/retention.py`, **consumed rows only** — an undelivered push-back must outlive the window that would have destroyed it |
| `note_index` | 012 (+035, 039) | `retrieval/vector_index.py` | derived and rebuildable (`make reindex`, which now also heals a model change); rows for deleted notes are not removed |
| `session_owners` | 013 (+021, 043, 046 index) | `agent/session_store.py` | — (survives its session's pruned history; BACKLOG) |
| `user_preferences` | 015 | `agent/preferences.py` | — |
| `predictions` | 016 | `science/calc/calibration.py` | — |
| `subscriptions` | 017 (+029) | `agent/subscriptions.py` | deleted on unsubscribe |
| `session_turns` | 018 | `agent/session_store.py` | released at turn end; a leased row, so an abandoned claim expires |
| `artifact_blobs` | 019 | `science/calc/postgres_artifacts.py` | `durable/artifact_eviction.py`, by idle window and size budget (both off by default) |
| `calculation_artifacts` | 019 | `science/calc/postgres_artifacts.py` | cascades from `artifact_blobs` |
| `plan_approvals` | 020 (+034) | `agent/plan_approval_store.py` | — (consumed rows are marked, not removed) |
| `job_records` | 023 (+033, 049, 055, 057) | `durable/job_record_store.py` | **refused**: the table exists because a durable run's result used to expire with Temporal's history and take a campaign's evaluation record with it (D-157) |
| `observations` | 025 | `memory/observations.py` | stale rows retired by status, not deleted |
| `note_proposals` | 027 (+036) | `kg/proposal_store.py` | — |
| `measurements` | 030 | `science/calc/calibration.py` | — |
| `bo_campaigns` | 031 | `science/bo/campaign_record_store.py` | — |
| `bo_suggestions` | 031 (+037) | `science/bo/campaign_record_store.py` | cascades from `bo_campaigns` |
| `audit_anchors` | 032 | — (retired with the audit hash chain; nothing writes it) | never — the table is empty and kept only because the schema is forward-only |
| `turn_costs` | 033 | `agent/turn_cost_store.py` | — |
| `document_files` | 037 (+040, 041) | `ingest/documents/index.py` | `ingest/documents/sync.py`, mark-and-sweep: rows a *complete* crawl did not see are removed, so a file deleted from the share leaves the index. Never swept on an incomplete crawl — an unmounted share and an empty one look identical |
| `document_chunks` | 037 (+038, 040, 041) | `ingest/documents/index.py` | cascades in effect from `document_files`: the same sweep deletes any *cutting* — `(doc_id, chunking_key)` — no remaining file row claims, and `upsert` applies the identical predicate to the documents it writes. Derived and rebuildable — dropping both tables and re-running the sync reconstructs them |
| `tool_result_blobs` | 042 | `api/tool_results.py` | `durable/retention.py`, by `created_at` (`retention_tool_results_days`). 0 by default like every other window, so **an operator who has not stated one lets this grow** — and at up to a row per tool call it grows fastest of the three. It holds no record of anything (the answers are in `calculation_results` and `job_records`), so a plain age cutoff is the whole policy it needs |
| `tool_result_links` | 042 | `api/tool_results.py` | cascades from `tool_result_blobs` |
| `reaction_records` | 052 (+053, 056) | `ingest/eln/records.py` | **nothing bounds it, deliberately** — one row per ELN entry (~1 kB), upserted by id, so the corpus tracks the source system and an amendment overwrites rather than appends. A row is the *only* readable form of a run (D-2026-08-25), so pruning one deletes a result; a deployment mirroring a 3M-entry ELN should expect a few GB and no growth beyond what the ELN itself holds |
| `result_publications` | 050 | `publish/outbox.py` | `durable/retention.py`, by `delivered_at` (`retention_result_publications_days`, 0 by default). **`state = 'delivered'` only**, and the predicate is the policy rather than an optimization: a delivered row is a receipt for a result that now lives both here and in an external store, so pruning it loses nothing — while a `pending` or `failed` row is the only record that something has **not** been published, and sweeping that on a clock would turn a results-store outage into a silent gap |
| `structures` | 047 | `science/calc/postgres_structures.py` | **refused**: a row is the geometry a `structure_id` names, and that address is handed to chemists, written into notes and taken as an argument by the next calculation (D-2026-08-21). Pruning it would break a handle rather than reclaim anything — the same coordinates are inside the `calculation_results` payload one table over, which D-011 already refuses to prune. Rows are a few kB and deduplicated by content |

## Three things the shape of this table will not tell you

**Six tables in this database are not in the table above, and cannot be.** `checkpoints`,
`checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations` are created by
`AsyncPostgresSaver.setup()` (`agent/checkpointer.py`); `store` and `store_migrations` by
`AsyncPostgresStore.setup()` (`agent/scratchpad.py`), which also creates `store_vectors` and
`vector_migrations` when it is built with an `index_config` — this deployment builds it without one,
so those two do not exist here. None is created by a file in this directory, so
`tests/test_schema_inventory.py` — which pins the table to exactly what the migrations create —
would call a row for them a phantom. That absence is not free: they hold every session's turn
state, they are the tables nobody reviews because they appear in no migration, and nothing disposed
of them for as long as they existed. `durable/retention.py` now prunes the checkpoints by **thread**
(`retention_checkpoints_days`) — a checkpoint chains to its parent, so a thread expires whole when
its newest checkpoint does — and `agent/leaver.py` erases them per actor. `checkpoint_migrations` is
the checkpointer's own version ledger and is never touched, the standing `schema_migrations` has.

Being outside this directory also kept them outside `grants/app_privileges.sql`, and that one was a
live second-deploy outage rather than a documentation gap: the reconciliation opens with
`REVOKE ALL ON ALL TABLES IN SCHEMA public`, which reaches these too and strips even the owning
role's own DML, while the enumerated re-grants below named none of them. A first install survives
it — the tables do not exist yet when the file runs — and the *second* `helm upgrade` takes every
turn down at its first checkpoint write. They are now granted explicitly, each guarded on its own
existence, and `tests/test_database_privileges.py` derives the same set from the installed
distributions so a table upstream adds in a minor bump fails the check instead of inheriting
`GRANT SELECT` and being found as a write outage.

**There are three foreign keys in the whole schema** (`calculation_artifacts` → `artifact_blobs`,
`bo_suggestions` → `bo_campaigns`, `tool_result_links` → `tool_result_blobs`), each one where a
cascade is load-bearing — a link row outliving its bytes would hand a caller a reference to
nothing. Everything else is
associated by a shared id with no constraint — including the four `session_*` tables, which is why
pruning one of them does not touch the others and why the **Disposal** column has to be read per
row rather than per subsystem.

**`grants/` is applied by `make db-grants`, not by `make db-migrate`.** The migration set runs each
file exactly once, tracked by checksum, which is right for a schema change and wrong for a grant: a
grant is a reconciliation between a schema that keeps growing and a runtime role that may be created
at any point, so run-once semantics would leave every later table ungranted and break the
application on first use of it. It re-runs on every deploy, after the migrations, and no-ops where
no `chemclaw_app` role exists (D-2026-08-05-append-only-by-grant-not-by-contract).
