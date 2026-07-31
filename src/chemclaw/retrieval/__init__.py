"""Reading the system's own data back out: the retrievers, and the report harness over them.

Two halves joined by one contract. **Retrieval** is `retrievers` (graph substring, dense embedding,
lexical FTS, structural similarity), `hybrid` (Reciprocal Rank Fusion across them) and
`vector_index` (the derived dense + lexical index, in-memory or Postgres). **The report harness**
(`harness`) is the deep-research pattern turned inward — decompose → fan-out → verify → cite →
synthesize, over accumulated internal notes instead of the web, producing a sectioned, fully-cited
draft that is PR-gated like every other agent-generated artifact.

`evidence` is what joins them: the harness core knows *only* the retriever contract and no concrete
source (gate G6), and every `EvidenceChunk` carries a back-reference to its source note, so an
unsupported claim is discarded rather than written. Sources are attached through
`chemclaw.ingest.sources`, not by editing anything here — no new data store.

The package was called `report` before D-148 and kept that docstring until D-155, which is why its
name and its opening line disagreed for a while.

**`chemclaw.memory` is next door and stays there.** This package answers "what do we have on this?";
memory answers "what did past work teach us?" — campaign chains, failure modes, distilled playbooks.
The two sound alike, which is exactly why the split is recorded rather than left to be re-derived.
"""
