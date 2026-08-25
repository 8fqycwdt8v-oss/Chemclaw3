# Attach Databricks: a vector store, a warehouse driver, and the things underneath

The deployment wants Databricks to hold its data — the ELN, a Pistachio patent-reaction corpus, the
document share and the knowledge graph — searchable by embedding and written to. Before this,
`grep -ri databricks` over the tree returned nothing; so did `pistachio`.

The shape of the work turned out to be small, because two seams already existed for it, each with
one implementation and a documented "add another" path. What made it worth an ADR is what attaching
the *second* implementation to each seam exposed about the first.

Decision record: `docs/decisions/D-2026-08-25-a-lakehouse-arrives-on-two-seams-not-one.md`.

## The plan, as executed

- [x] **The vector store.** `retrieval/vectors/databricks.py` over Mosaic AI Vector Search: three
      methods, client late-bound through `importlib`, absent from `pyproject.toml`, provider name in
      the registry and the `Literal`.
- [x] **The score conversion**, which is the part that fails quietly — Databricks ranks by
      `1/(1 + d²)` over Euclidean distance and the seam's contract is a cosine.
- [x] **The warehouse driver.** `ingest/eln/warehouse/databricks.py`: `Row.asDict()`, `?` markers,
      the connection-field mapping documented, `private_key_env`/`role` refused rather than dropped.
- [x] **The dialect leak.** `VECTOR_COSINE_SIMILARITY` and `?::VECTOR(FLOAT, n)` move off `sql.py`
      onto `Warehouse.vector_dialect`; Snowflake's strings unchanged.
- [x] **`eln-databricks` manifest**, exercised against a fixture row like `eln-snowflake`.
- [x] **Notes join the seam** — `ExternalVectorNoteIndex`, plus the `retire_absent` prune
      `NoteIndex` never had, plus the grant that prune needs.
- [x] **Pistachio** — retrieve-only, ranked in an index and resolved in SQL, argued against D-089.
- [x] ADR, ledger row, three package READMEs, `DEFERRED.md` row, `.env.example`.
- [x] `make lint type test` green; `datasource-validate --construct` and `connector-validate` pass.

## Review

**Three things this found that were not the task.**

1. `warehouse/sql.py` argued in its own docstring that dialect facts belong on the connection, and
   then hardcoded one vendor's vector vocabulary. A second driver is what made that visible; the fix
   is a move, not a branch, and Snowflake's emitted statements are byte-identical after it.
2. `NoteIndex` had no delete and `reindex_notes` never pruned. Harmless while every vector sat in a
   Postgres table nobody bills per row; not harmless the moment the dense half can live elsewhere.
   Fixing it improves the pgvector deployment too — the old behaviour was an accumulation nothing
   reclaimed.
3. `tests/test_database_privileges.py` caught the grant that prune needed. A split-principal
   deployment would have hit `InsufficientPrivilege` on the first reindex after a note was deleted.

**Two things the plan got wrong and the tree corrected.**

- The plan proposed a `store=` constructor argument on the retriever for test injection.
  `tests/test_warehouse_binding.py` refused it, correctly: the registry splats a manifest's whole
  `config:` block into that signature, so every parameter there is something a manifest can set.
- The plan proposed `tests/test_upstream_surface.py` entries for the two vendor shapes. That file's
  assertions import their package unconditionally, and these clients are deliberately not installed
  — the Protocol slice in each adapter is the pinned shape instead.

**Measured, not asserted** (`ORTHOGONAL_SCORE`, `cosine_from_score`, and the two compositions):

| what | result |
| --- | --- |
| cosine 1.0 / 0.9 / 0.5 through `1/(1+d²)` and back | exact to floating point |
| cosine 0.0 / −0.5 / −1.0 | dropped by the `> 0` floor |
| raw score for a cosine of 0.0, if unconverted | 0.3333 — a hit, ranked above a true 0.3 |
| narrow scope over 200 documents, pre-filtered | 1 hit (the eligible one) |
| the same, post-filtered | **0 hits** — the recall defect being avoided |
| reference store vs adapter over one corpus | identical ids, identical order, Δscore 1.1e-16 |

**What is not proven.** No Databricks workspace exists here, so three vendor facts are pinned
against documentation and a fake: the score formula, the `{"column": [values]}` filter form, and
`similarity_search`'s response shape (read tolerantly for exactly that reason). `DEFERRED.md` names
a workspace as the trigger and the score formula as the one to check first, because it is the only
one of the three that fails by mis-ranking rather than by erroring.

**Two failing tests are environmental, not this change**: `tests/test_prompt_caching.py`'s two live
tests run only when `API-KEY` is set and this sandbox's credential returns 401.
