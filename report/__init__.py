"""On-demand report / deep-research harness over the system's own data (plan Phase 5b).

The deep-research pattern (decompose → fan-out → verify → cite → synthesize) turned inward:
it synthesizes a sectioned, fully-cited report from the accumulated internal notes instead of
the web. A stable, source-agnostic harness core (`report.harness`) knows only the retriever
contract (`report.evidence`); concrete retrievers (`report.retrievers`) are thin adapters over
existing layers (the knowledge graph, fingerprint search) — no new data store. Every claim
must trace to a source note, unsupported claims are discarded, and the draft is PR-gated.
"""
