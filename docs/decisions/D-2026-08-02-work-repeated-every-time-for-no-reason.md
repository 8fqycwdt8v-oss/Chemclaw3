# D-2026-08-02-work-repeated-every-time-for-no-reason — Two costs proportional to the whole corpus, paid on every run

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-004 (git-markdown is the source of
truth, the index is derived), D-151 (durable history compaction), D-011 (persist once, never
recompute)

## Context

Two independent hot paths did strictly more work than the request in front of them justified, both
in the shape "recompute the whole thing every time, when almost none of it changed":

- `retrieval/vector_index.py::reindex_notes` loaded every note under `knowledge_dir` and called
  `embed_texts` over every note's full text on **every** call, with no notion of "changed since last
  index". `durable/note_index.py::NoteReindexWorkflow` runs this on a schedule (`note_reindex_
  schedule_minutes`, default 60), so a stable corpus paid one embedding call per note, per hour,
  against the deployment's LLM endpoint, forever. `chemclaw.kg.graph` already solved the identical
  problem for the graph indexer — a stat fingerprint (`path, mtime_ns, size`) per note file, diffed
  against a cache — and nothing reused it for the vector index.
- `agent/session_store.py::PostgresHistoryProvider._compact` issued a `COUNT`, a full unbounded
  `SELECT` of the session's entire stored history, and a `plan_compaction` pass over all of it on
  **every** `save_messages` call once the row count passed `agent_durable_compaction_min_rows` — on
  top of the full read MAF already performs before the model call for the same turn. The connection
  was held open across `plan_compaction`'s CPU work, pinning a pooled connection for the length of
  the plan.

Both are "work repeated every time for no reason": the corpus (notes; stored messages) is
overwhelmingly unchanged between adjacent runs, and the code re-derived the answer from scratch each
time anyway.

## Decision

**1. `reindex_notes` is incremental by default, keyed off a per-note stat fingerprint.**
`kg.graph.note_file_fingerprints` (`note id -> "mtime_ns:size"`) extracts the identical stat-only
scan `_dir_fingerprint` already performs for the whole-tree cache, keyed per note instead of folded
into one aggregate — the graph indexer's own pattern, reused rather than reinvented.
`NoteRecord.fingerprint` and a new `note_index.fingerprint` column (`infra/sql/035`) persist what
each note was embedded from; `NoteIndex.fingerprints()` reads it back. `reindex_notes` diffs current
against stored and calls `embed_texts` only for notes whose fingerprint moved (new, edited, or never
indexed). `full: bool = False` is the explicit escape hatch (`--full` on the CLI, `make
reindex-full`) for recovery from a corrupted index or an embedding-model change the fingerprint
cannot see — a *different* problem from the separately tracked backlog item about the index lacking
embedding-model identity, not solved by this change and not conflated with it. NULL fingerprint
(rows written before this migration) reads as "unknown" and is always re-embedded once, never
skipped as a stale match.

**2. `_compact` replans only on a fresh floor-bucket crossing, and releases the connection before
the CPU work.** `_crossed_new_compaction_bucket(count_before, count_after, floor)` is a pure function
of two row counts: `count // floor` buckets the row count into 0, 1, 2, …, and a replan only fires
when the insert just performed moved the count into a new bucket — not on every turn spent above the
floor. `count_before` is always derived from this call's own freshly-read `count_after` minus what it
just inserted, so nothing is persisted between calls; the turn-claim lease (D-121) is what makes that
derivation safe (no concurrent writer for the session mid-call). The `SELECT` + `plan_compaction`
pass, when it does run, is unchanged — same strategy, same protected-rows watermark, same result —
so what changed is *how often* the expensive path is attempted, not what it computes when it runs.
The connection opened for the `COUNT`+`SELECT` is released (the `async with` block exits) before
`plan_compaction`'s CPU work runs, and the write-back loop (`_UPDATE_MESSAGE` per row) became
`executemany`, matching the identical statement's use two methods above (P2 — the same "row-at-a-time
loop beside a batched twin" the plan flagged, fixed in the same file while touching this method).

## Measurement

Both are timed, per `tasks/lessons.md`'s rule that a performance change is worthless until it is
timed.

- **Reindex, 50-note offline corpus, `embed_texts` calls counted at the call site:**
  before — run 1 (cold) 50, run 2 (unchanged) **50**, run 3 (one note edited) **50**; after — run 1
  **50**, run 2 **0**, run 3 **1**. Reproduced end-to-end against the real 38-note `knowledge/`
  corpus through `make reindex` / `python -m chemclaw.retrieval.vector_index` with a real migrated
  Postgres (`infra/sql/035` applied): first run indexed 38, second run (nothing changed) indexed 0,
  `--full` re-indexed all 38 regardless.
- **Compaction bucket gate**, pure-function unit tests (`tests/test_session_store.py`) pin the
  invariant offline: below the floor never crosses; the turn that first reaches the floor crosses;
  staying in the same bucket (201 -> 202) does not; the next multiple (399 -> 400) crosses again.
  `tests/test_session_store.py::test_durable_compaction_bounds_a_long_session` (60 turns, real
  Postgres) still holds after the change — the row count still tracks the compaction window rather
  than the turn count, proving the gate did not weaken the actual bound, only how often it is
  attempted.

## Consequences

- Behavior is unchanged: the same notes end up indexed (same fingerprint diff, same embeddings), the
  same history ends up compacted (same `plan_compaction` call, same inputs, when it runs) — verified
  by the existing round-trip and boundedness tests passing unmodified, plus new tests asserting the
  call counts directly rather than only the end state.
- `note_index` gained one nullable column (`infra/sql/035`); no new `Settings` field, so no
  `.env.example` entry — `reindex_notes(full=...)` is a function/CLI parameter, not a deployment
  tunable.
- The postgres-backed tests in this change (`test_vector_index.py`, `test_session_store.py`) ran
  against a real migrated database in this sandbox (pgvector rebuilt from source for `bit_jaccard_
  ops`, per D-157's precedent) rather than skipping; CI runs them again against its own database.
