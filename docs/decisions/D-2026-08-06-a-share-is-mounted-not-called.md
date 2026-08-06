# D-2026-08-06-a-share-is-mounted-not-called — a classical file share becomes a corpus, and its AD group becomes an entitlement

**Status:** accepted

## Context

A deployment arrives with a decade of reports, presentations, spreadsheets and PDFs on a classical
Windows/SMB file server — >500k files, terabyte scale — reachable by anyone in one Active Directory
security group, and inside that group everyone sees everything. None of it was reachable from
ChemClaw3.

What was already true, and what was not:

- **The parsers existed and were good.** `agent/attachments.py` read PDF/DOCX/XLSX/PPTX/CSV/MD/TXT
  structurally and offline, and refused a scanned PDF *by name* rather than returning an empty
  document (D-089 §2).
- **No document corpus was indexed anywhere.** `note_index` holds one vector per whole note; there
  was no chunking and no sub-document coordinate. Attachments were session-scoped and in-memory.
- **The retrieve half of the `DataSource` seam fit exactly** — `SourceRetriever` → `EvidenceChunk`,
  fanned out by `gather_evidence`.
- **The ingest half did not.** `IngestHalf = ElnAdapter` is reaction-shaped (`map_to_ord →
  OrdReaction`), and a PowerPoint is not a reaction. `DEFERRED.md` had named this exact case
  ("Universal ingest abstraction — trigger: the first non-reaction-shaped source").
- **Authorization knew nothing about AD groups.** `Principal` carried `oid`/`upn`/`roles`; the
  `groups` claim was read nowhere, and retrieval had no identity filtering of any kind.

## Decision

### 1. The share is mounted, never called

The share is a read-only CIFS/SMB PersistentVolume on the background worker. Everything in
`chemclaw.ingest.documents` takes a POSIX path — the shape `eln-json`'s `export_dir` has had all
along.

The alternative, an SMB client in Python, would have bought a new dependency, a credential the
application holds, and a new network peer — which D-089 declines by name and
`tests/test_no_egress.py` enforces. Mounting needed no amendment to either: there is no host
literal to allow, because the code never speaks to a host. The mount credential belongs to the
PersistentVolume and is read by the CSI driver, so no key joins the chart's plain-secret set.

`readOnly: true` on both the volume and the mount, so "this system never writes to a site's file
share" is enforced by the kubelet rather than asserted in a docstring.

### 2. Retrieve-only data source, indexed by a core background job

`sources/sharedrive/datasource.yaml` declares a `retrieve:` half and nothing else;
`durable/document_sync.py` keeps its index fresh on the `background-jobs` queue.

This is precisely the shape `vector` and `lexical` already have — retrieve-only manifests whose
index `durable/note_index.py` maintains — so it needed no change to `sources/base.py`, no second
enable switch beside `CHEMCLAW_DATA_SOURCES` (D-018), and no widening of `IngestHalf`.

**The "universal ingest abstraction" was deliberately not built.** It has exactly one caller, and
CLAUDE.md's Rule of Three says an abstraction with one caller gets inlined. Its `DEFERRED.md`
trigger is updated to name the *second* non-reaction-shaped source rather than deleted, because
this sidesteps the deferral rather than closing it.

### 3. Not the PR-gate: these are evidence, not knowledge

`WarehouseVectorRetriever` is the precedent — it returns `EvidenceChunk`s citing `f"{name}:{key}"`
and writes no note. The PR-gate is the GxP line for what the *agent generates*; a report a chemist
wrote in 2019 is a pre-existing record, retrieved with a citation to the file and page it came from.

Concretely: `cli/backfill_corpus.py` PR-gates one note per document. At 500k files that is 500k pull
requests. It stays what it is — the right tool for a small curated folder someone wants *in* the
graph — and is not the path for a share.

### 4. The share's layout is a binding, not Python

Straight from `D-2026-08-04-the-schema-is-a-file`, one layer over: a warehouse's tables exist before
the adapter, and so does a file share's directory tree. Which folders hold project work, which
segment of a path is the project code, which extensions to open, what the size and chunk budgets
are, and which entitlement is required — all of it is data in `datasource.yaml`. Nothing in
`chemclaw.ingest.documents` names a folder, an extension list or a project code.

A deployment mounts its own manifest folder first on `CHEMCLAW_DATA_SOURCES_DIR` and changes zero
repository files. The shipped manifest describes the test fixture share, so it is exercised on every
run and cannot rot.

### 5. Security trimming is a source-level entitlement, in the one vocabulary that exists

Everyone on the share sees everything, so per-file ACLs would be ceremony with no effect. The rule
that matches reality is: **a caller not in the share's AD group gets nothing from this source at
all.**

The system already has exactly one entitlement vocabulary — the `roles` frozenset in
`identity_context`, read by `authz.py`, `skill_access.py` and every manifest gate. So an AD group
joins *that set* rather than getting a second one beside it. One opt-in flag
(`entra_group_claims_as_roles`) unions the token's `groups` claim into it, which makes both tenant
wirings work with no further code:

- the AD group assigned to an Entra **app role** → arrives as a normal `roles` value (needs Entra
  ID P1 for group-based assignment); or
- the **`groups` optional claim** emitted → arrives as a group object-id, unioned in.

A second collection would mean every gate deciding separately whether it also consults groups, which
is the shape a rule takes just before it stops being enforced in one of the places it was written
(`D-2026-08-05-one-rule-in-three-places-is-three-rules`).

**A gated share refuses when there is no identity to check** — `require_actor`'s reject-if-absent
rule applied to a corpus. An **ungated** share (`required_roles: []`) has nothing to verify and needs
no actor; demanding an identity in order to check an empty requirement would block the report
workflow for no security benefit.

## Three properties the implementation is arranged around

**Identity is the content, not the path.** `doc_id` is the stable hash of the *parsed text*, so the
same report filed into four project folders is one set of chunks and one embedding call, and a
rename or a move is free. It is `backfill_corpus.note_for_document`'s rule and D-011's, applied to
embeddings — and on a share this size it is the difference between an affordable corpus and an
unaffordable one.

**Deletion is a mark-and-sweep, and the sweep is guarded.** Every crawl restamps what it saw; the
sweep removes what it did not — but only after a *complete* crawl with no failed root. A CIFS mount
that dropped presents to `scandir` as an empty directory, indistinguishable from "somebody deleted
everything", and of the two possible mistakes re-indexing is recoverable and deleting is not. The
sweep's reference clock is read from the *database*, not the worker, so worker-versus-database skew
cannot make freshly-marked rows look older than the run that marked them.

**Nothing is skipped silently.** Scanned PDFs and legacy binary Office (`.doc`/`.xls`/`.ppt`) are a
population on a decade-old share, not a corner case. Both are counted — per extension — and reported
in `SyncReport`. Silence would be read as "the share held nothing else", which is the one answer
that is never true. This is why `ScannedDocumentError` is its own exception type.

## Scale: what >500k files actually costs

| Stage | Estimate |
| --- | --- |
| Crawl (stat-only, no reads) | ~500k `scandir` entries — minutes |
| After the extension allowlist | 30–50% parseable → ~200k candidates |
| After content-hash dedup | −20–40% → ~130k unique documents |
| Chunks at 1800 characters | ~8 average → **~1M chunks** |
| Vectors | 1536-dim float4 ≈ 6 KB → **~6 GB** plus the HNSW index |
| Embedding calls | ~1M — the dominant cost, and the only one worth controlling |

The controls are all in the binding: staged rollout by `roots`, `max_file_bytes`, the extension
allowlist. `make share-estimate SHARE=<source>` walks the real mount, reads nothing, and reports the
per-extension counts before a single embedding is bought — because nobody should learn the size of
that bill by watching it arrive.

## Consequences

- One new subpackage (`ingest/documents/`), one migration (`037`), one workflow, one manifest, one
  config flag and five bounds. No change to `sources/base.py`, the registry, `gather_evidence`, or
  the retrieval interface.
- **The parsers moved down.** `chemclaw.ingest` may not import `chemclaw.agent`
  (`tests/test_layering.py`), so the extractors now live in `ingest/documents/parse.py` and
  `agent/attachments.py` imports them. Reading a PDF is an ingest concern that an upload happens to
  use; moving them rather than copying them keeps one parsing implementation with two callers.
- **The chat pod stays clean.** `retriever.py`, `binding.py` and `formats.py` import nothing
  third-party, so building the retrieve half loads no document-parsing library —
  `tests/test_datasource_isolation.py` asserts it in a subprocess, and the counterfactual was
  verified to fail.
- **The report workflow gets no evidence from a gated share.** `durable/report_workflow.py` calls
  `active_retrieve_sources()` where no identity contextvar is set, so the gate correctly denies.
  Right by construction and *silent*, so it is a `BACKLOG.md` row for propagating `requested_by`
  into retrieval context.
- Two defects were found by the tests rather than by production: the in-memory index's lexical score
  was a raw token count and violated `EvidenceChunk`'s `[0, 1]` contract (now bounded on
  `DocumentHit` itself, where the two backends meet one DTO), and `deduplicated` counted only
  cross-pass duplicates, reporting zero for the commonest case there is — the same report filed into
  two project folders on one crawl.

## Alternatives rejected

**An SMB client in Python.** A dependency, a credential in the application, and a new network peer,
to obtain a file handle the operating system already offers. D-089 declines external hosts, and
`tests/test_no_egress.py` would have needed amending for something a volume mount gives free.

**Per-file ACL indexing with query-time trimming.** The share does not have per-file permissions;
building the machinery would have implied a security property the underlying system does not
provide, which is worse than not having it.

**Widening `IngestHalf` to a union.** One caller. It is the abstraction CLAUDE.md says to inline
until a second real caller exists, and the `vector`/`lexical` precedent already gave the shape.

**Two data sources, `sharedrive-vector` and `sharedrive-lexical`.** The share is one corpus behind
one entitlement; splitting it would have duplicated the binding and the AD group across two
manifests. The two legs are fused inside the source with the same `reciprocal_rank_fusion` the
cross-source layer uses, because after fusing a cosine with a `ts_rank` only *position* is
comparable (`D-2026-08-01-a-cap-that-starves-a-source`).

**Re-using `note_index`.** `retrievers._chunks_from_hits` drops any hit whose note is not on local
disk, which is every document on the share — the same trap `WarehouseVectorRetriever` documents.
`SourceRetriever` is the right altitude; `NoteIndex` is not.
