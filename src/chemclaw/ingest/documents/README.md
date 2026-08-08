# `chemclaw/ingest/documents/` — a mounted file share as cited evidence

**Responsibility:** make the documents on a classical SMB/CIFS share answerable — crawl them,
read them, chunk them, embed them, and retrieve them with a citation to the file and page they
came from. Declared by a `datasource.yaml` binding, driven by
`chemclaw.durable.document_sync`, and read back through the ordinary `SourceRetriever` seam.

The corpus a real site arrives with is not an ELN. It is a decade of reports, decks, spreadsheets
and PDFs on a shared drive, gated by one AD group, and until this package existed none of it was
reachable. `docs/decisions/D-2026-08-06-a-share-is-mounted-not-called.md` is the record; the
operator-facing guide is `docs/guides/sharedrive-concept.md`.

## The modules

| Module | What it is | Imports third-party document libraries? |
| --- | --- | --- |
| `formats.py` | the closed extension → content-type allowlist | no |
| `binding.py` | the share's layout as a validated document (`DocumentShareBinding`) | no |
| `crawl.py` | the stat-only walk: what is worth opening, and where to resume | no |
| `chunk.py` | cutting a parsed document while keeping its page/slide/sheet coordinate | no |
| `index.py` | `DocumentIndex` + the in-memory reference and the pgvector backend | no |
| `retriever.py` | `ShareDocumentRetriever` — the retrieve half, and the entitlement gate | **no** |
| `parse.py` | the parsers themselves (`pypdf`, `python-docx`, `python-pptx`, `openpyxl`) | yes |
| `sync.py` | the crawl→diff→parse→embed→sweep loop | via `parse` |

That last column is the layout, not an accident. `chemclaw.ingest.sources.registry` builds every
active retrieve half in the **chat** pod, so `retriever.py` importing a PDF reader would put the
whole document-parsing stack in the process that serves conversations — the defect D-118 measured
for `calc` and fixed with its `specs.py`/`results.py` split.
`tests/test_datasource_isolation.py` holds it in a subprocess.

## Five things to know before changing anything here

**The share is mounted, never called.** Everything takes a POSIX path. There is no SMB client, no
credential in Python, and no new egress host (D-089) — a CIFS PersistentVolume is the platform's
job. That is also why `tests/test_no_egress.py` needed no amendment.

**Nothing here writes.** Not to the share (no code path opens a file for writing), and not to the
knowledge graph. These documents are pre-existing human-authored records, so they are *evidence*
retrieved with a citation; the PR-gate exists for what the agent generates.
`chemclaw.cli.backfill_corpus` is the other choice — one PR-gated note per document — and it is
right only for a small curated folder someone wants *in* the graph. At 500k files it would be
500k pull requests.

**A vector is only good for the model that made it.** Every stored chunk records the embedding
configuration that produced its vector (`embedding_key`, migration 038). Changing the model does
not move any file's fingerprint, so before `D-2026-08-06-a-vector-is-only-good-for-the-model-that-made-it`
a model swap re-embedded nothing and left the table holding a mix of two models' vectors — with no
error anywhere. `sync.reembed_stale` refreshes them from the `content` stored beside each vector,
so the fix is a database-to-database pass that never touches the share and runs even when the mount
is down.

**And a chunk is only good for the chunking that cut it.** The same argument one step out
(`chunking_key`, migration 040): `chunk_chars` and `chunk_overlap_chars` decide what text each
vector describes, and until D-2026-08-08 neither of the crawl's two gates could see them change —
so re-tuning them re-chunked *nothing*, and when a re-chunk did happen for another reason it left
the finer cutting's higher ordinals stranded, which `reembed_stale` then adopted as current. Both
gates now compare it, and a document's chunks are replaced rather than merged.

**Identity is the content, not the path.** `doc_id` is the hash of the parsed text, so the same
report in four project folders is one set of chunks and one embedding call, and a rename is free.
It is `backfill_corpus.note_for_document`'s rule and D-011's, applied to embeddings.

**Deletion is a mark-and-sweep, and the sweep is guarded.** A crawl marks every file it saw; the
sweep removes what it did not — but only after a *complete* crawl with no failed root. A CIFS
mount that dropped presents to `scandir` as an empty directory, and of the two possible mistakes,
re-indexing a corpus is recoverable and deleting one is not.

**Nothing is skipped silently.** A decade-old share is full of scanned PDFs (no text layer) and of
`.doc`/`.xls`/`.ppt` files no library here can open. Both are counted — per extension — and
reported in `SyncReport`. Silence would be read as "the share held nothing else", which is the one
answer that is never true.

## The entitlement gate

Getting onto the share is an AD-group decision; once on it, everyone sees everything. So the
enforcement that matches reality is not per-file ACLs but: *a caller not in the share's group gets
nothing from this source at all.* `ShareDocumentRetriever._entitled` checks the binding's
`required_roles` against the turn's roles — the one entitlement vocabulary `authz.py`,
`skill_access.py` and every manifest gate already share. An AD group reaches it either by being
assigned to an Entra app role, or as a group object-id under
`CHEMCLAW_ENTRA_GROUP_CLAIMS_AS_ROLES`.

A **gated** share refuses when there is no identity to check (`require_actor`'s reject-if-absent
rule, applied to a corpus). An **ungated** one has nothing to verify and needs no actor — the
distinction is deliberate: demanding an identity in order to check an empty requirement would
block the report workflow for no security benefit.
