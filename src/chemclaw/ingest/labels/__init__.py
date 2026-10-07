"""The I/O half of reaction labelling: building rows, calling the labeller, draining the corpus.

`science/` may import only `chemclaw.core`, so the vocabulary, row models and index live in
`chemclaw.science.labels`; everything that talks to something (the record-phase builder, the MCP
client for the labelling server, the drains over the index) lives here.
"""
