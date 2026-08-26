# `chemclaw.ingest.labels` — reaction labelling, the I/O half

**Responsibility:** everything about the reaction-label index that touches something outside the
process — the record-phase builder that reads a canonical `OrdReaction`, the MCP client for the
labelling server, and the drains that walk the index filling in what is missing.

The models, the vocabulary and the index itself are in `chemclaw.science.labels`. The split is not
taste: `tests/test_layering.py` lets `science/` import `chemclaw.core` and nothing else, and lets a
connector bundle import `science/` but not `ingest/`. So anything a search tool needs has to be
over there, and anything that needs `OrdReaction`, an MCP session or a Temporal activity has to be
here. `chemclaw.ingest.documents` is split on the same line.

| Module | What it does |
| --- | --- |
| `record.py` | builds the record phase of a label row from an `OrdReaction` — the form with agents **kept**, and why |
| `labeller.py` | the MCP client for `Chemclaw3-mcp:servers/rxnlabel` — and why it never derives the version |
| `merge.py` | the fill-what-is-missing rule: `provides` is never a skip |
| `enrich.py` | one bounded drain pass, and why a batch failure retries reaction by reaction |
| `corpus.py` | one keyset page of a bulk reaction corpus into the record phase |

There is deliberately no retriever here. A corpus source's `retrieve:` half is
`ingest.eln.warehouse.retriever:WarehouseVectorRetriever` — an embedding search over the same
table — and the corpus drain finds its binding through the manifest rather than through that
object, because a source declares exactly one retrieve callable and the two seams answer different
questions.
