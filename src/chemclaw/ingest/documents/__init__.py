"""Documents on a mounted file share, made answerable as cited evidence.

`crawl` walks the share, `parse` reads each document structurally, `chunk` cuts it while keeping the
page or slide it came from, `index` stores the chunks in pgvector, and `retriever` answers from them
behind the entitlement its manifest declares.

- **The share is mounted, never called.** Everything here takes a POSIX path: no SMB client, no
  credential in Python, no egress host (D-089).
- **Nothing here writes to the knowledge graph.** Pre-existing human-authored documents are evidence
  retrieved with a citation, not notes; `chemclaw.cli.backfill_corpus` is the path for a curated
  folder that belongs in the graph.
"""
