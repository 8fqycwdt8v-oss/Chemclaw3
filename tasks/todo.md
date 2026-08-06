# Task: make a classical SMB share answerable in ChemClaw3

Branch: `claude/sharedrive-chemclaw3-access-3okznv`. Decision:
`docs/decisions/D-2026-08-06-a-share-is-mounted-not-called.md`; operator guide:
`docs/guides/sharedrive-concept.md`.

Ask: a decade of reports, presentations, spreadsheets and PDFs on an on-prem Windows/SMB file
server (>500k files, TB scale), gated by one AD security group — and inside that group everyone
sees everything.

---

## Plan — the five decisions everything else follows from

- [x] **Mounted, never called.** A read-only CIFS PersistentVolume, so the code takes a POSIX path:
      no SMB client, no credential in Python, no new network peer. D-089 needed no exception and
      `tests/test_no_egress.py` needed no amendment.
- [x] **A retrieve-only DataSource with a core background indexer** — the shape `vector` and
      `lexical` already have. No widening of `IngestHalf` (it is reaction-shaped; a PowerPoint is
      not a reaction), no core edit to `sources/base.py`, no second enable switch beside
      `CHEMCLAW_DATA_SOURCES` (D-018).
- [x] **Evidence, not the PR-gate.** These are pre-existing human-authored records, cited on
      retrieval. `cli/backfill_corpus.py` PR-gates one note per document — at 500k files that is
      500k pull requests, so it stays what it is.
- [x] **The share's layout is a binding**, not Python (D-2026-08-04's argument, one layer up).
      Nothing in the package names a folder, an extension list or a project code.
- [x] **Security trimming is a source-level entitlement** in the one role vocabulary that already
      exists. One opt-in flag unions the Entra `groups` claim into it, so both tenant wirings (app
      role, or group object-id) work with no second code path.

## Build

- [x] Moved the document parsers down to `ingest/documents/parse.py` (+ `formats.py`), imported by
      `agent/attachments.py`. Forced: `ingest` may not import `agent` (`test_layering.py`), and a
      second parser set is the duplication CLAUDE.md forbids. Reading a PDF is an ingest concern an
      upload happens to use, so this is the right direction anyway.
- [x] `ingest/documents/`: `binding`, `crawl`, `chunk`, `index`, `sync`, `retriever`, `README.md`.
- [x] `infra/sql/037_document_index.sql` — content-addressed `document_chunks`, path-addressed
      `document_files`; inventory row and grants (full DML — the sweep genuinely deletes).
- [x] `durable/document_sync.py` on `background-jobs`: bounded chunks, heartbeating,
      continue-as-new. Registered in the worker, in `publish._BAD_DATA_TYPES`, and in
      `schedules.py` conditionally on a share actually being enabled.
- [x] `sources/sharedrive/datasource.yaml` — describes the test fixture share, so it cannot rot.
- [x] Config: five bounds plus `entra_group_claims_as_roles`, mirrored in `.env.example`.
- [x] `api/auth.py` — union the `groups` claim; a claim *overage* is logged, never read as "no
      groups" (it would quietly deny the users with the most access).
- [x] `cli/sync_share.py`, `make share-estimate` / `make share-sync`.
- [x] Helm: read-only PVC mount, on the background worker alone.
- [x] Docs: ADR + ledger row, operator guide, `ingest/README.md`, `ARCHITECTURE.md`, `BACKLOG.md`,
      `DEFERRED.md` (trigger updated, not deleted — this *sidesteps* the deferral).

## Verify

- [x] `tests/test_document_share.py` — 24 tests over a real fixture tree of real documents: crawl
      filters, resume without double counting, unmounted-is-loud, symlink escape, page integrity
      through chunking, dedup, no-re-embed, edit detection, **prune only on a complete crawl**,
      entitlement (in / out / absent / ungated), citations, filters, never-raises.
- [x] `tests/test_datasource_isolation.py` — building the share retriever loads no document parser,
      asserted in a subprocess. Counterfactual verified: importing `parse` makes it fail.
- [x] `tests/test_research_tools.py` — measured `{"graph": 5, "sharedrive": 5}` under the cap. A
      flat cap in config order would read `{"graph": 10}` (D-2026-08-01).
- [x] `tests/test_auth.py`, `test_schedules.py`, `test_helm_chart.py` — group claim and overage,
      the conditional schedule, the read-only mount on the worker only.
- [x] `make lint type test` green; `datasource-validate --construct` and `prose-validate` green.
- [x] CLI smoke-tested against a fixture mount: dry run reported 2 candidates and `.doc: 1`.

---

## Review

**Two defects the tests found that a reading would not have.** Both surfaced only because the
assertions counted things rather than describing them:

1. `InMemoryDocumentIndex.search_lexical` returned a raw shared-token count (2.0), violating
   `EvidenceChunk`'s `[0, 1]` contract. The Postgres path hid it, because `ts_rank` happens to be
   in range — it would have surfaced as a `ValidationError` in a chat turn. Fixed at the boundary
   where two backends meet one DTO: `DocumentHit.score` is now bounded, and the Postgres value is
   clamped, since `ts_rank` sums per-term weights and is only *usually* below 1.
2. `deduplicated` counted only content already on record from an earlier pass, so it reported
   **zero** for the commonest case there is — the same report filed into two project folders on one
   crawl. Now `len(parsed) - len(fresh)`.

**Two places the design changed while building**, both wrong at scale rather than wrong in
principle:

- Prune began as "diff the stored path list against the crawl", which needs every path for the
  source in memory on every chunk of a drain. Replaced with mark-and-sweep on `indexed_at`, one
  statement per chunk — and the sweep's reference clock then had to move to the *database*, because
  the mark is a database `now()` and worker-versus-database skew would delete freshly-marked rows.
- The crawl cursor began as "the last accepted file". Everything skipped between that and where the
  chunk stopped would be re-examined next pass and tallied twice, inflating a drain's skip counters
  — and an inflating counter is worse than none, because it is read as a measurement. It is now the
  last entry *examined*.

**What is deliberately not built**, each with its row and trigger in `DEFERRED.md`: OCR, legacy
binary Office conversion, per-file ACLs, and the universal ingest abstraction (still one caller).
Three live edges are in `BACKLOG.md`: identity propagation into scheduled reports, a run against a
real CIFS mount, and HNSW recall under a filtered document search.
