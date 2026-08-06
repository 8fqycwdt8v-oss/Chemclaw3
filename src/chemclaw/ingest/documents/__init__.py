"""Documents on a mounted file share, made answerable as cited evidence.

The corpus a real site arrives with is not an ELN: it is a decade of reports, decks, spreadsheets
and PDFs on a classical Windows/SMB share. This package is how that becomes searchable —
`crawl` walks it, `parse` reads each document structurally, `chunk` cuts it while keeping the page
or slide it came from, `index` stores the chunks in pgvector, and `retriever` answers questions
from them behind the entitlement its manifest declares.

Two boundaries are load-bearing and easy to erase by accident:

- **The share is mounted, never called.** Everything here takes a POSIX path. There is no SMB
  client, no credential in Python, no new egress host (D-089) — the mount is the platform's job,
  exactly as `eln_export_dir` is a directory rather than an ELN client.
- **Nothing here writes to the knowledge graph.** These documents are pre-existing human-authored
  records, so they are *evidence*, retrieved with a citation. The PR-gate exists for what the agent
  generates. `chemclaw.cli.backfill_corpus` is the other choice — one PR-gated note per document —
  and it is the right one only for a small curated folder someone wants *in* the graph.
"""
