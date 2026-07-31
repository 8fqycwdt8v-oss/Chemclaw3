# `chemclaw.retrieval` — finding things, and reporting on them

**Responsibility:** reading the system's own accumulated data back out. Two halves that share a
contract:

- **Retrieval** — `retrievers.py` (the concrete sources: graph substring, dense embedding, lexical
  FTS, structural similarity), `hybrid.py` (Reciprocal Rank Fusion across them), `vector_index.py`
  (the derived dense + lexical index over notes, in-memory or Postgres).
- **The report harness** — `harness.py`, the deep-research pattern turned inward: decompose →
  fan-out → verify → cite → synthesize, over internal notes instead of the web. `evidence.py` is
  the contract joining the two: an `EvidenceChunk` **must** carry a back-reference to its source
  note, so every claim in a report is traceable.

They are one package because the harness is defined against the retriever interface and nothing
else — it knows no concrete source (gate G6). Sources are attached through the `DataSource` seam in
`chemclaw.ingest.sources`, not by editing anything here.

## The boundary against `memory/`

`memory/` is next door and stays there. Retrieval answers *"what do we have on this?"* against the
graph; memory answers *"what did past work teach us?"* — campaign chains, failure modes, distilled
playbooks, superseded findings. The two sound alike and are not: merging them would put eleven
modules a level deeper to save a word. Recorded in D-155 so it does not get "tidied" later.

## Not layer 1

Nothing here may import `chemclaw.agent`. The harness is called *by* orchestration; it does not
orchestrate. `tests/test_layering.py` runs each retrieval module in a clean interpreter and asserts
`chemclaw.agent` never appears.
