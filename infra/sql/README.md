# `infra/sql/` — the schema

**Responsibility:** every table this system has, in ordered `.sql` files applied by
`chemclaw.core.migrate` against a `schema_migrations` ledger with per-file checksums (D-034). The
schema is **forward-only and additive** — no migration may drop, rename, truncate or delete
(D-2026-08-04-the-schema-only-goes-forward, enforced per file by
`tests/test_migrations_are_additive.py`). New SQL is a new numbered file; an applied file is never
edited, because the ledger flags the changed checksum as drift.

That check asks **three** questions, because destroying data, ending the rollback and being
replayable are different things with different answers
(D-2026-08-08-a-rollback-that-is-not-a-schema-step,
D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated). Destroying data is refused
outright. Leaving the *previous image* unable to write — `SET NOT NULL` on an existing column, a
dropped or replaced key, an `ALTER COLUMN … TYPE` — is refused unless the migration is listed in
`_REVIEWED_ROLLBACK_BREAKS` with the statements read and an ADR saying what an operator does instead
of "deploy the previous image". Two registers sit beside it for what a pattern cannot reach: a
column whose *meaning* the previous image must honour (`_REVIEWED_SEMANTIC_BREAKS`, judged rather
than matched — the guard reads statements, and a lease and a comment are the same eleven tokens),
and an `ADD CONSTRAINT` that no `DROP CONSTRAINT IF EXISTS` precedes, which aborts a *replay* rather
than a rollback (`_REVIEWED_REPLAY_BREAKS`).

**All three are listed at the foot of this file**, and `tests/test_schema_inventory.py` checks those
lists against the registers in both directions. No count is given with them: the count is what went
stale, twice. This paragraph said "exactly one" while the set held four, then "four" while it held
five — and the one it omitted was `088_turn_cost_identity.sql`, the newest and the only one bearing
on a rollback of the current release, under a sentence claiming the list was "derived from that set".

`grants/` is not part of that set and is invisible to the runner's non-recursive glob by
construction. See the note at the bottom.

## The tables

One row per table. **Written by** names the module that owns its writes — the store, not every
caller. **Disposal** is what bounds its growth, and a blank there means nothing does. The register
of record is `durable/retention.py`'s `_NOT_PRUNED`, which names **every** table in this schema and
what bounds it — including, in its own words, the ones where nothing does and no decision is on
file. (This sentence used to delegate to a `docs/planning/BACKLOG.md` row instead. That row was
closed and deleted, as the rules for that file require, and the delegation outlived it — so the
column an operator reads before writing a cleanup script pointed at nothing.)

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
| `calculation_results` | 001 (+019 `compute_seconds`, 024 indexes, 048 `structure_id`, 090 `epoch`) | `science/calc/postgres_store.py` | **refused**: evicting a cached result silently converts a hit into a recomputation, potentially an hours-long CREST search (D-011). Bounded by cost policy, not by a clock |
| `molecule_fingerprints` | 002 (+004, 046 index, 084 scan-order index, 094 `definition` in the key) | `science/fingerprints/store.py` | — |
| `reaction_fingerprints` | 003 (+004, 046 index, 063 `source` + `(source, id)` key, 094 `definition` in the key) | `science/fingerprints/store.py` | — |
| `reaction_labels` | 051, 086, 091 | `science/labels/store.py` | derived and rebuildable: drop it and re-run the corpus drain plus the label backfill |
| `reaction_species` | 051 | `science/labels/store.py` | derived and rebuildable; a species the source amended away is deleted with its reaction's record phase |
| `corpus_molecules` | 054 | `ingest/labels/corpus.py` | derived and rebuildable: refilled by re-draining the corpus |
| `corpus_reactions` | 071 (+094 key) | `ingest/labels/corpus.py` | derived and rebuildable: refilled by re-draining the corpus |
| `corpus_cursors` | 072 | `ingest/labels/cursor.py` | — (one row per append-only corpus source; deleting a row is the supported way to force a full re-walk) |
| `audit_events` | 006 (+010, 011, 026, 044, 045, 059) | `agent/audit_store.py` | **refused**: the trail is the record of who ran what, and disposing of it is a policy decision for whoever owns that record rather than an age cutoff in a cleanup job. `prev_hash`/`row_hash`/`chain_version` are retired columns, unwritten, at their defaults |
| `sync_cursors` | 007 | `ingest/eln/cursor.py` | — (one row per ingest source; bounded by the source count) |
| `session_messages` | 008 (+022, 026, 043, 046 `message_shape` check, 067 `message_original`) | `agent/session_store.py` | `durable/retention.py`, per session through the pairing closure (D-145). The in-line compaction on write this row used to name went with the engine that needed it. `message_original` needs no disposal of its own: it dies with its row, and its population cannot grow — nothing has written a `maf`-shaped row since M6, so the set that can ever carry one was fixed then (D-2026-08-27-a-conversion-that-cannot-be-rolled-back-is-not-a-pre-upgrade-step). An operator who has trusted the conversion may `SET message_original = NULL` to reclaim it, which is the deliberate act of giving up the rollback |
| `session_events` | 009 (+014, 028) | `agent/session_events.py` | `durable/retention.py`, **consumed rows only** — an undelivered push-back must outlive the window that would have destroyed it |
| `note_index` | 012 (+035, 039) | `retrieval/vector_index.py` | derived and rebuildable (`make reindex`, which now also heals a model change); rows for deleted notes are not removed |
| `session_owners` | 013 (+021, 043, 046 index, 092 sort key) | `agent/session_store.py` | `durable/retention.py`, **last** and only once nothing is left to reopen: past the conversation window, no session-scoped row anywhere, no live turn lease (`D-2026-08-27-a-session-nobody-can-reopen-is-disposable`). The row is what makes a session reopenable *and* what every session-scoped sweep starts from, so it is disposed of behind everything it keys, never in front of it |
| `user_preferences` | 015 | `agent/preferences.py` | — |
| `predictions` | 016 | `science/calc/calibration.py` | — |
| `subscriptions` | 017 (+029) | `agent/subscriptions.py` | deleted on unsubscribe |
| `session_turns` | 018 | `agent/session_store.py` | released at turn end, and a lease keyed by `session_id` is overwritten in place rather than duplicated, so it does not accumulate. The lease a crashed worker never released is swept with its session's `session_owners` row, in the same transaction; a **live** lease is never touched |
| `artifact_blobs` | 019 | `science/calc/postgres_artifacts.py` | `durable/artifact_eviction.py`, by idle window and size budget (both off by default) |
| `calculation_artifacts` | 019 | `science/calc/postgres_artifacts.py` | cascades from `artifact_blobs` |
| `plan_approvals` | 020 (+034) | `agent/plan_approval_store.py` | refused: kept through erasure (consumed rows are marked, not removed) |
| `job_records` | 023 (+033, 049, 055, 057, 061, 085 search trigrams) | `durable/job_record_store.py` | **refused**: the table exists because a durable run's result used to expire with Temporal's history and take a campaign's evaluation record with it (D-157) |
| `pending_requests` | 076, 079 | `durable/pending_store.py` | **refused**: the attribution for an answer that released a durable workflow, retained for the reason `plan_approvals` is — a settled row names who asked somebody to run, review or deliver something and who answered. Bounded by how often a *person* is asked something, which is human-paced and orders below the session tables; this is not `session_events`, where one turn writes many rows |
| `commitments` | 074 | `ingest/commitments/store.py` | **refused**: a mirror that converges rather than accumulating (upserted on `(source, external_id)`), bounded by the size of the portfolio it reflects. A clock cutoff would delete the delivered rows that make "what did we ship last quarter" answerable; staleness is reported by `observed_at`, not pruned |
| `effects` | 075, 078 | `durable/effect_ledger.py` | **refused**: what this system changed in a system it does *not* own, and who approved it when the change could not be undone. The change is still standing on the far side and outlives any window this could be pruned on. Bounded by how often this system acts outside itself — which no job in this repository does at all |
| `observations` | 025 (+062 index) | `memory/observations.py` | stale rows retired by status, not deleted |
| `note_proposals` | 027 (+036, +058) | `kg/proposal_store.py` | refused: kept through erasure |
| `measurements` | 030 (+093 key) | `science/calc/calibration.py` | — |
| `bo_campaigns` | 031 | `science/bo/campaign_record_store.py` | refused: kept through erasure |
| `bo_suggestions` | 031 (+037) | `science/bo/campaign_record_store.py` | cascades from `bo_campaigns` |
| `audit_anchors` | 032 | — (retired with the audit hash chain; nothing writes it) | never — the table is empty and kept only because the schema is forward-only |
| `turn_costs` | 033 (+060 `outcome`/`error_code`/`model`/counts/`ttft_seconds`, +069 `compacted`/`context_unreducible`, +082 the knowledge dimensions, +083 those four made nullable, +087 `estimated_tokens`, +088 `turn_id` as the primary key) | `agent/turn_cost_store.py` | refused: kept through erasure **082 adds what a turn looked at, cited and wrote back** — `retrieval_calls`, `capture_calls`, `answer_confidence`, `review_required`, `notes_cited`. This row could say what a turn spent and how it ended and not whether it consulted the record at all, which two reviews of the knowledge loop each had to answer with a bespoke script; none of it is recoverable afterwards, because the event stream ends with the turn and `session_messages` holds prose rather than which tool ran. **All five are nullable and must stay so**, which took 083 to finish: 082 got it right for `answer_confidence` — the verifier can be off and the answer-shape gate sets `review_required` with no score, so a 0 would read as "graded, and graded terrible" for a turn that was never graded — and wrong for the other four, whose `NOT NULL DEFAULT` backfilled every row already in the table with "answered without consulting the record", the most interesting value they can take. A separate migration rather than an edit, because `core/migrate.py` keys on a checksum of the statements. **087 adds `estimated_tokens`, and it is a separate column on purpose**: `stream_options.include_usage` reports a request's usage on the terminal chunk only, so a turn the client abandons mid-message is billed by the gateway and reported by nobody — measured, a turn killed after 12 token frames wrote 0/0 while the gateway logged a real request. The budget meters the measured tokens *plus* the estimate, because a cost guard has to bind on the whole bill; the ledger keeps them apart so an inferred number can never pass for a provider's, and so a reader can ask what fraction of this table is inference. **088 moves the primary key off `correlation_id`**, because the front door *adopts* that id off the request when the caller sends a well-formed `X-Chemclaw-Correlation-Id` — so `ON CONFLICT (correlation_id) DO UPDATE` let a client that repeats one header collapse its whole history to one row: measured, two turns of 900,000 and 1,000 input tokens left a single row reading 1,000. `turn_id` is minted per record and crosses no wire; `correlation_id` stays as an indexed, **non-unique** join column, which is what a template run's one-row-per-step writing already needed it to be (D-2026-09-06-an-id-a-caller-chooses-is-not-a-key) |
| `document_files` | 037 (+040, 041) | `ingest/documents/index.py` | `ingest/documents/sync.py`, mark-and-sweep: rows a *complete* crawl did not see are removed, so a file deleted from the share leaves the index. Never swept on an incomplete crawl — an unmounted share and an empty one look identical |
| `document_chunks` | 037 (+038, 040, 041) | `ingest/documents/index.py` | cascades in effect from `document_files`: the same sweep deletes any *cutting* — `(doc_id, chunking_key)` — no remaining file row claims, and `upsert` applies the identical predicate to the documents it writes. Derived and rebuildable — dropping both tables and re-running the sync reconstructs them |
| `tool_result_blobs` | 042 | `api/tool_results.py` | `durable/retention.py`, by `created_at` (`retention_tool_results_days`). 0 by default like every other window, so **an operator who has not stated one lets this grow** — and at up to a row per tool call it grows fastest of the three. It holds no record of anything (the answers are in `calculation_results` and `job_records`), so a plain age cutoff is the whole policy it needs |
| `tool_result_links` | 042 | `api/tool_results.py` | cascades from `tool_result_blobs` |
| `reaction_records` | 052 (+053, 056, 066 `retracted_at`, 068, 081 `reaction_id`) | `ingest/eln/records.py` | **nothing bounds it, deliberately** — one row per ELN entry (~1 kB), upserted by id, so the corpus tracks the source system and an amendment overwrites rather than appends. A row is the *only* readable form of a run (D-2026-08-25), so pruning one deletes a result; a deployment mirroring a 3M-entry ELN should expect a few GB and no growth beyond what the ELN itself holds. `retracted_at` (066) is **reserved and unread**: the tier that would have written it was deleted on review, because with a producer wired the withdrawn run still came back from the unfiltered evidence sweep and from `similar_reactions` — see `D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry`, and 068 restates the column's comment to say so **081 restores the bare-`reaction_id` index 056 dropped**: 052 made that column the primary key and said "the PK carries the lookup", 056 widened the key to `(ingest_source, reaction_id)` and a composite B-tree cannot answer a predicate on its second column, so every turn-time read by bare id became a full scan — measured at 500,000 rows, 38.9 ms/3,955 buffers for one `read()` and a 76.3 ms parallel seq scan for `eligible()` at the shipped depth, against 0.10 ms and 1.10 ms with the index |
| `experiment_protocols` | 073 (+080 `updated_at DESC`) | `protocols/store.py` | **nothing prunes it, deliberately** — one header row per experiment design (a few hundred bytes), and it is the record of an experiment somebody may still run or may already have run. `durable/retention.py` states that refusal rather than implying it, and the grant is what enforces it: the runtime role has INSERT and UPDATE and no DELETE. Growth is one row per design a chemist opens, which is bounded by how much laboratory work exists. **080 adds the index the unfiltered listing needs**: 073's two both lead with a predicate column and the default `GET /protocols` has no predicate, so at 200,000 rows it measured a parallel seq scan and a sort — 2,396 buffers and 23.3 ms, against 6 buffers and 0.052 ms for the same query with a status filter |
| `experiment_protocol_status_events` | 077 | `protocols/store.py` | **nothing prunes it, and INSERT-only by grant** — one row per deliberate lifecycle move, carrying the head revision it was made against, the actor, and the chemist's own reason. It exists because the header's `status` describes the *head* and `store.advanced()` retires an `approved` or `executed` status the moment a revision lands, so nothing else can answer "which document did somebody sign off on". Growth is a handful of rows per design — bounded by how many times a person changes their mind about one experiment |
| `experiment_protocol_revisions` | 073 | `protocols/store.py` | **nothing prunes it, and INSERT-only by grant** — every revision of every design, agent-authored and human-authored alike. A revision is what an expert's correction of a generated protocol *is* (`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`), so a credential that could rewrite one could erase the signal the table exists to keep, and a sweep that pruned one would delete the diff rather than a duplicate. `document` is a whole design as JSONB — kilobytes for a plate, so a heavily-revised 384-well campaign is the largest realistic row here. It cascades from `experiment_protocols`, which is itself never deleted, so the cascade is unreachable |
| `result_publications` | 050 (+089 `claimed_at`) | `publish/outbox.py` | `durable/retention.py`, by `delivered_at` (`retention_result_publications_days`, 0 by default). **`state = 'delivered'` only**, and the predicate is the policy rather than an optimization: a delivered row is a receipt for a result that now lives both here and in an external store, so pruning it loses nothing — while a `pending` or `failed` row is the only record that something has **not** been published, and sweeping that on a clock would turn a results-store outage into a silent gap. **089 adds `claimed_at`, the delivery lease**: the claim spends its attempt and commits before delivering, so `SKIP LOCKED` — which excludes overlapping *transactions* — let two drains 0.3 s apart both deliver one row. A leased row stays `pending`, which is true of it, so the exhausted-row reaper handles a claimer that died holding one and no second timer exists to be forgotten |
| `ingest_rejections` | 065 | `ingest/rejections.py` | **self-bounding, and the only table whose disposal is its own writer's**: at most 1,000 rows per source, the least recently refused evicted in the same transaction as a write (D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask). A corpus with one systematically broken field is exactly the case that would otherwise write a row per record per run, and it is the case where the newest refusals are the informative ones. No retention sweep touches it |
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

## What a rollback and a replay cannot assume

Both lists below are checked against `tests/test_migrations_are_additive.py`'s registers, in both
directions, by `tests/test_schema_inventory.py`. Read them before a `helm rollback`, and before
re-running the migrations against a database that already carries the schema. Each entry names the
ADR carrying the reading behind it.

### Migrations that end "deploy the previous image"

- `041_document_chunk_identity.sql` — `SET NOT NULL` on `document_files.chunking_key` and
  `document_chunks.chunking_key`, plus a replaced primary key. The previous image's file writes and
  its `ON CONFLICT` on the old key both fail (D-2026-08-08-a-rollback-that-is-not-a-schema-step).
- `056_reaction_record_identity.sql` — the `reaction_records` key widens to
  `(ingest_source, reaction_id)` (D-2026-08-26-a-transcription-is-keyed-by-its-source).
- `058_note_proposal_superseded.sql` — flagged for the `DROP CONSTRAINT` text and reviewed as **not**
  a break: the drop-and-re-add *widens* the state `CHECK`, and the previous image writes only states
  it already allowed (D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed).
- `063_reaction_fingerprint_source.sql` — the `reaction_fingerprints` key gains `source`
  (D-2026-08-27-a-fingerprint-is-keyed-by-its-source).
- `088_turn_cost_identity.sql` — the `turn_costs` primary key moves off `correlation_id` onto
  `turn_id`, so the previous image's `ON CONFLICT (correlation_id)` no longer plans
  (D-2026-09-06-an-id-a-caller-chooses-is-not-a-key).
- `089_result_publication_lease.sql` — **judged, not matched.** One nullable column, additive by
  every pattern, and the previous image keeps writing the table — *without the lease*. A pre-089
  pod's claim ignores `claimed_at` and re-claims a row a new-pod drain is mid-delivering, spending
  an attempt on a delivery already in flight. Quiesce publishing for the duration of the rollback;
  afterwards `UPDATE result_publications SET attempts = 0, claimed_at = NULL WHERE state =
  'pending';` returns double-spent attempts, and also resurrects any genuinely exhausted row
  (D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated).
- `091_reaction_label_confidence_precision.sql` — flagged for the `ALTER COLUMN … TYPE` text and
  reviewed as **not** a break: the conversion *widens* `reaction_labels.confidence` from `REAL` to
  `DOUBLE PRECISION`, every stored value survives it exactly, and the previous image writes a Python
  float into the column as before. A *narrowing* conversion is a different matter and may not be
  exempted at all — it destroys data, which the guard's other bucket refuses outright
  (D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated).

- `092_session_owners_updated_at.sql` — **judged, not matched.** One nullable column, additive by
  every pattern, and it does not end the rollback: the pre-092 image derives the sidebar's sort key
  from `max(session_messages.created_at)` and ignores the column entirely. What it does not do is
  *maintain* it, so a session taking its first turn during the rollback window comes back with
  `updated_at IS NULL` and is missing from `GET /sessions` until it is spoken in again. Re-run the
  migration's own backfill by hand to restore it
  (D-2026-09-09-a-sort-key-a-page-cannot-prune-is-a-scan).
- `093_measurement_source.sql` — the `measurements` primary key gains `source`, the fourth table to
  be keyed that way after 051, 056 and 063. Nothing is destroyed by restoring the previous image —
  the widening added a column to the key rather than removing information — but a row written under
  the new key whose `source` is not `chemist-reported` is unreachable to the old reader's
  two-column lookup. Run the migration forward again
  (D-2026-09-09-a-measurement-is-keyed-by-who-measured-it).
- `094_fingerprint_definition_identity.sql` — the `definition` joins the primary key on
  `molecule_fingerprints`, `reaction_fingerprints` and `corpus_reactions`, so a superseded
  generation is *shelved* rather than deleted. Unlike 056, 063 and 093 this one stops the previous
  image writing at all: its `ON CONFLICT (id)` / `(source, id)` no longer plans against the widened
  key, so every fingerprint and corpus-reaction write fails with `InvalidColumnReference`. Roll
  forward, or re-add the old key by hand
  (D-2026-09-09-a-definition-change-shelves-a-row-it-does-not-delete).
### Migrations that are not re-runnable, and the recipe for each

Re-running the whole set is how a restored database whose `schema_migrations` ledger is older than
its tables is recovered. Two files abort that run — the runner sends everything in one transaction,
so nothing after the failure applies either. Apply the recipe first; both were verified end to end,
after which every tracked file replays clean against a fully populated database. A count is
not written here: it was verified at 90 files and read 94 four waves later, and the sentence
is about the two recipes, not about how many files there happen to be.

- `046_review_hardening_indexes.sql` — `ADD CONSTRAINT session_messages_shape_known` with no drop
  above it, so a replay aborts with `DuplicateObject: constraint "session_messages_shape_known" for
  relation "session_messages" already exists`. The constraint is `NOT VALID`, so re-adding it costs
  no table scan. Run first:
  `ALTER TABLE session_messages DROP CONSTRAINT IF EXISTS session_messages_shape_known;`
- `058_note_proposal_superseded.sql` — it *does* drop first, without `IF EXISTS`, so it replays
  against a restore and aborts against a database built by hand without that constraint:
  `UndefinedObject: constraint "note_proposals_state_known" of relation "note_proposals" does not
  exist`. The recipe puts the constraint back so the bare drop finds one, and re-adds the
  *post*-058 form — identical to what the file itself re-adds, so it cannot fail on data 058
  already permits, where the pre-058 form would reject any row already holding `superseded`.
  Both recipes are unconditional and idempotent: run them without first working out which arm
  the database is in. Run first:
  `ALTER TABLE note_proposals DROP CONSTRAINT IF EXISTS note_proposals_state_known; ALTER TABLE note_proposals ADD CONSTRAINT note_proposals_state_known CHECK (state IN ('open', 'merged', 'rejected', 'failed', 'superseded'));`
