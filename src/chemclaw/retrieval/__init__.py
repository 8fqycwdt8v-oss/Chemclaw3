"""Reading the system's own data back out: the retrievers, and the report harness over them.

**Retrieval** is `retrievers` (graph substring, dense, lexical FTS, structural similarity),
`hybrid` (Reciprocal Rank Fusion) and `vector_index` (the derived dense + lexical index). **The
report harness** (`harness`) is the deep-research pattern turned inward (decompose, fan out,
verify, cite, synthesize) over internal notes, producing a fully cited draft written straight
through with its provenance.

`evidence` joins them: the harness knows only the retriever contract, and every
`EvidenceChunk` carries its source note, so an unsupported claim is discarded. Sources attach
through `chemclaw.ingest.sources`. `chemclaw.memory` is separate: this package answers "what do
we have on this?", memory "what did past work teach us?".
"""
