# `chemclaw.retrieval` — finding things, and reporting on them

**Responsibility:** reading the system's own accumulated data back out. Two halves that share a
contract:

- **Retrieval** — `retrievers.py` (the concrete sources: graph substring, dense embedding, lexical
  FTS, structural similarity), `hybrid.py` (Reciprocal Rank Fusion across them), `vector_index.py`
  (the derived dense + lexical index over notes, in-memory or Postgres), `external_note_index.py`
  (the same index with its dense half in a vector store — see below), and `vectors/` (the seam that
  lets those vectors live outside Postgres at all).
- **Condensation** — `condense.py`, for the case where retrieval returns more whole protocols
  than fit one model call. A protocol is atomic and is never split; the comparison is
  `memory/comparison.py`'s table (the same one `optimization_campaign_note` renders, so the
  turn-time artifact and the recorded note cannot disagree), and the only model call is over the
  half of a record that is prose. `agent/protocol_tools.py` resolves the citations and exposes it.
- **The report harness** — `harness.py`, the deep-research pattern turned inward: decompose →
  fan-out → verify → cite → synthesize, over internal notes instead of the web. `evidence.py` is
  the contract joining the two: an `EvidenceChunk` **must** carry a back-reference to its source
  note, so every claim in a report is traceable.

They are one package because the harness is defined against the retriever interface and nothing
else — it knows no concrete source (gate G6). Sources are attached through the `DataSource` seam in
`chemclaw.ingest.sources`, not by editing anything here.

## Where the note vectors live

`default_note_index()` picks one of two shapes from `vector_store_provider`, exactly as
`default_document_index()` does one layer over. Under `pgvector` — the default —
`PostgresNoteIndex` answers ranking, eligibility and the row in a single statement, and there is
nothing to delegate. Under any other provider, `ExternalVectorNoteIndex` moves **only** the dense
half: the text, the `tsvector`, the file fingerprint and the embedding key stay in `note_index`,
because `ts_rank` is not a thing a vector database offers.

It is a subclass: four of `NoteIndex`'s five methods overridden, `search_lexical` inherited
untouched. Three of the four are the dense half, and the fourth is the one worth knowing about —
`fingerprints` namespaces the stored `embedding_key` by the collection, so that switching provider
does not leave every row claiming a vector the new store has never held. Still smaller than the
document twin: a note is embedded whole, so the point id *is* the note id and there is no citation
to resolve.

**`reindex_notes` retires notes deleted from disk** (D-2026-08-25). It did not use to, on the
argument that a stale row is harmless — true while every vector sat in a Postgres table nobody bills
per row, and false once the dense half can live in a store that no other sweep reaches. The prune
runs before the "nothing changed" exit, because a run whose only news is a deletion has nothing to
embed and must still act.

## The boundary against `memory/`

`memory/` is next door and stays there. Retrieval answers *"what do we have on this?"* against the
graph; memory answers *"what did past work teach us?"* — campaign chains, failure modes, distilled
playbooks, superseded findings. The two sound alike and are not: merging them would put eleven
modules a level deeper to save a word. Recorded in D-156 so it does not get "tidied" later.

## Not layer 1

Nothing here may import `chemclaw.agent`. The harness is called *by* orchestration; it does not
orchestrate. `tests/test_layering.py` runs each retrieval module in a clean interpreter and asserts
`chemclaw.agent` never appears.
