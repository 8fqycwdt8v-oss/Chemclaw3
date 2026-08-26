"""The I/O half of reaction labelling: building rows, calling the labeller, draining the corpus.

Split from `chemclaw.science.labels` along the line `tests/test_layering.py` draws: `science/` may
import `chemclaw.core` and nothing else, so the vocabulary, the row models and the index live
there — where a connector bundle can also reach them, since `connectors -> ingest` is not an
allowed edge. Everything that talks to something lives here: the record-phase builder (which needs
`OrdReaction` and the note-id rule), the MCP client for the labelling server, and the drains that
walk the index. `chemclaw.ingest.documents` is split on exactly the same line and for the same
reason.
"""
