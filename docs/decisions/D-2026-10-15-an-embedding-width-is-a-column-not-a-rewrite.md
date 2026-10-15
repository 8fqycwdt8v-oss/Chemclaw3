# D-2026-10-15-an-embedding-width-is-a-column-not-a-rewrite — a model with another vector width gets a sibling column built from `embedding_dim`, and a claimed, resumable job moves the corpus into it

**Status:** accepted · **Date:** 2026-10-15 · **Builds on**
`D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it` (a vector is read only under the
key that made it) and `D-2026-08-04-the-schema-only-goes-forward` (no migration rewrites in place).

## Context

Programme item W7.2. `note_index.embedding` and `document_chunks.embedding` were created
`vector(1536)` (012, 037), and `embedding_dim` was validated against a constant naming that width, so
a model of any other width (768, 1024) could not be used at all: the config refused it. A
`vector(N)` column keeps its width for life, and an HNSW index needs one.

What already existed and is kept: each row carries `embedding_key`
(`provider:endpoint:d<N>:model`, 038 and 039), dense reads match it exactly, and a changed key makes
`reembed_stale` and `reindex_notes` re-embed. So a same-width model change was already healed. What
was missing is the width, a way to see a corpus holding two configurations, and a re-embed that two
workers, or a killed one, cannot do twice.

## Options

1. **`ALTER COLUMN … TYPE vector(N)` in a migration.** One column, no new names. `ALTER … TYPE` is
   refused by the additive rule because it ends the previous image's writes, the HNSW index must be
   dropped and rebuilt over the whole table in one transaction, and a rollback has no data to go
   back to. It is the in-place rewrite this programme forbids.
2. **An unsized `vector` column with a partial expression index per width.** pgvector indexes
   `(embedding::vector(768))` with a `WHERE` on the width. It needs the same `ALTER … TYPE` to
   unsize the existing column, every query must repeat the cast and the predicate for the planner to
   find the index, and a mixed table then holds vectors no operator can compare without a cast.
3. **A sibling column and HNSW index per width, `embedding_<N>`, built from `embedding_dim`; the
   original keeps its name and is the 1536 column.** Additive: a new nullable column on a table,
   empty at creation so its index builds over nothing. A deployment that never changes width
   changes nothing and re-embeds nothing. Reads and writes use the column the setting names.
4. **A table per width, or per model.** Clean separation, but the lexical half, the catalogue
   and the file join live beside the vector in one row, and a second table duplicates them or splits
   every query across two.

## Decision

Option 3. `infra/sql/130_embedding_width.sql` defines `ensure_embedding_width(width)`, which adds
`embedding_<width>` and its HNSW cosine index to `note_index` and `document_chunks` when they are
absent and does nothing for 1536 or when they exist. `core/migrate.py` calls it on every run with
`settings.embedding_dim`, because the width is a setting that can change between releases while a
migration file runs once; the chart's pre-upgrade hook therefore builds the column for the width
the release states, and an unchanged width costs one catalogue lookup and no lock. A width pgvector
cannot index (over 2000) is refused by config where anything writes the note index, and skipped with
a warning by the migrator elsewhere.

A row has its live vector in the column its key's width names, and is read only under the live key.
Moving to a new model is: set `embedding_provider`, `embedding_model` and `embedding_dim` on the
release; the hook builds the column; `make reembed` starts `EmbeddingReembedWorkflow`. It claims
stale chunks in batches with `FOR UPDATE SKIP LOCKED` (rows locked from the read to the write, so a
killed worker changes nothing and two workers embed disjoint chunks), runs the note reindex, empties
the superseded width's vectors in batches, and ends by counting what is not under the live key.
Until it finishes dense retrieval ranks only rows already moved, lexical and graph retrieval are
unaffected, and `/readyz` says `embedding_corpus: mixed` (degraded, still ready). A configured
width with no column is `vector width behind config` (unready), because nothing can read or write it.

The per-row record of model and width is `embedding_key`, decoded by
`core/vector_schema.py::parse_embedding_key`; no second pair of columns, which would be written
beside the key and could disagree with it.

Rolling back is deploying the previous image. It reads `embedding` and treats every row under the
new key as stale, which its own re-embed heals; the sibling columns stay, unread. A contract step
that drops them is a deliberate operation, not a migration.

## Consequences

- A width change is one release plus one command, and is resumable: the job's state is the rows'
  keys, so any run converges.
- The original column cannot be dropped by a migration, so a deployment that leaves 1536 keeps an
  empty column and index until an operator drops them. The job empties them; it cannot free the
  index pages without a `VACUUM`.
- An external vector store is unaffected: it writes `NULL` to the original column at any width.
- Not covered: widths over 2000 (a `halfvec` expression index is the route), and re-embedding the
  note index from Postgres notes rather than the checkout (it follows `NoteStore`).

Held by `tests/test_embedding_width.py` (old-width database with data migrated forward, new width
fresh, HNSW used, mixed corpus reported, two real processes and a SIGKILLed one against one table),
`tests/test_service.py` (`/readyz` for both verdicts) and `tests/test_config.py`.

Revisit when: a supported embedding model is wider than 2000 dimensions, or a deployment has changed
width twice and its dead sibling columns are measurable in `pg_total_relation_size`.
