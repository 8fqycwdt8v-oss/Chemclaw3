# Task: the embedding configuration is part of a vector's identity

Branch: `claude/sharedrive-chemclaw3-access-3okznv` (restarted from `main`; #142 is merged).
Decision: `docs/decisions/D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it.md`.

Follow-up to the mounted-share build. Reviewing its vectorization path afterwards turned up two
gaps with one root: **the schema stores vectors and records nothing about which embedding
configuration produced them.**

---

## Plan

- [x] **One constant.** `_NOTE_INDEX_VECTOR_DIM` → `SCHEMA_VECTOR_DIM`: there is no coherent
      deployment where two vector columns in one database have different widths, and two private
      constants for one fact is how they come to disagree.
- [x] **The width guard on the constructors**, not in config — a share's name is chosen by the
      deployment, so no `NOTE_INDEX_SOURCES`-style name set can find one, and `core` may import no
      sibling to ask. Residual stated: it fires at first use, not process start.
- [x] **`embedding_config_key()`** extracted in `core/embeddings.py`, with the in-memory cache
      rebuilt on top of it — one definition of "which configuration makes a vector".
- [x] **Migration 038**: `document_chunks.embedding_key` + its index. NULL = unknown = stale.
- [x] **`reembed_stale()`** reading stored `content`, drained by the workflow *before* the crawl
      and by the CLI. Never touches the share.
- [x] `known_documents()` keyed on the configuration too, so a copy arriving under a new path
      cannot inherit a superseded model's vector.
- [x] Config `document_reembed_batch_size`, `.env.example`, ADR + ledger, guide, package README.

## Verify

- [x] `tests/test_document_share.py` — 30 tests. The load-bearing one **deletes the share tree**
      before re-embedding, so if the pass ever starts needing the mount, a test says so.
      Plus: second pass is free (counted), a batch of 1 converges, a stale document is re-embedded
      even when its content is already on record, and a bad `embedding_dim` is refused at
      construction naming both numbers.
- [x] **Counterfactual measured**, not assumed: reverting `known_documents` to key on presence
      alone makes `test_a_stale_document_is_re_embedded_even_when_its_content_is_already_known`
      fail. The test discriminates.
- [x] `make lint type` green; `prose-validate`, `datasource-validate`, the migration and config
      suites green.

---

## Review

**Why not the cheap fix.** A `--full` flag, or a documented "drop the two tables and re-run", is a
manual step performed by someone who already knows about the problem — and this problem raises no
error at all. A remedy gated on already knowing does not close a silent defect. Storing the key
makes the corpus self-healing, which is the only shape that works when nobody is watching.

**Why the guard could not go where the last one went.** The note-index width check lives in the
config validator because `vector`/`lexical` are shipped names it can enumerate. I assumed the same
shape would work for shares and it cannot: the name is the deployment's, and asking "is a share
enabled?" means importing its retrieve half, which `core` may not do. Naming that constraint took
longer than writing the guard.

**Fixed in passing:** the previous change had inserted the document-share config block between the
vendored-dataset comment and the field it describes.

**Still open** (`BACKLOG.md`, unchanged): identity propagation into scheduled reports, a run
against a real CIFS mount, HNSW recall under a filtered search, and re-reading refused files.
