# D-062 — F10-A: hybrid retrieval — dense + lexical entry points, RRF fusion (D-A10)

**Context.** Retrieval was graph traversal + binary structural fingerprints: no dense-semantic and no
lexical rank, so a note sharing neither a substring nor a wikilink with the query was unreachable.
This executes and extends the planned-but-unbuilt F8-T2.

**Decision.** `agents/embedding_provider.py` is the one embedding seam (`hash` offline / internal
`openai_compatible`). `report/vector_index.py` (`012_note_index.sql`) is a derived, rebuildable
pgvector + `tsvector` index over notes with in-memory + Postgres backends. `VectorRetriever` +
`LexicalRetriever` join `gather_evidence` via the F7 source registry (`vector`/`lexical` keys —
registry membership is the enable switch, D-018). `report/hybrid.py::reciprocal_rank_fusion` fuses
the per-source rankings under `retrieval_mode="hybrid"`; graph expansion stays the reasoning path
(D-004 intact — the new retrievers are *entry points* into the graph, never a replacement).

**Consequence.** Default `retrieval_mode="graph"` + `hash` embedder + `vector`/`lexical` not in
`data_sources` = today's flat union, unchanged. Git-markdown stays the source of truth; the index is
derived. A scheduled reindex activity is a documented follow-up (today `make reindex`/CLI populate).

**Result.** `make lint type test` green. Tests: `test_embedding_provider`, `test_vector_index`,
`test_hybrid_retrieval`, `test_config`.
