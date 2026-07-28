# 13 — Storage, result persistence & knowledge substrate: gap analysis

Companion to `09-knowledge-management-gaps.md` (which audited *retrieval*) and
`12-capability-gap-analysis.md` (which audited *capability*). This one audits the layer
underneath both: **what the system writes down, what it throws away, and what it can never
reconstruct.**

The storage substrate grew one migration at a time — nineteen files in `infra/sql/`, each
individually well-reasoned — and had never been read as one system. The question that
prompted this audit is narrow and expensive: a DFT or GFN calculation produces far more than
the number we persist, and every one of those by-products is currently deleted while being
exactly the input that makes the *next* calculation cheap.

---

## 1. What exists

**Postgres is the only durable store.** There is no object store, no blob column (before
D-124), no filesystem artifact area, and no second database.

| Migration | Table | Role |
|---|---|---|
| 001 | `calculation_results` | the calculation cache; `key` PK, `result JSONB` |
| 002/003/004 | `molecule_fingerprints`, `reaction_fingerprints`, `fingerprint_definition` | `bit(2048)` + HNSW `bit_jaccard_ops` |
| 006/010/011 | `audit_events`, `audit_revision`, `audit_hash_chain` | GxP audit trail, hash-chained |
| 007 | `sync_cursors` | ELN high-water marks, one per ingest source |
| 008/009/013/014/018 | `session_messages`, `session_events`, `session_owners`, `session_event_dedupe`, `session_turns` | durable conversation + job→session push-back |
| 012 | `note_index` | `vector(1536)` HNSW cosine + `tsvector` GIN — *derived*, rebuildable |
| 015/016/017 | `user_preferences`, `predictions`, `subscriptions` | prefs, predicted-vs-actual calibration, standing queries |
| 019 | `artifact_blobs`, `calculation_artifacts` | **new (D-124)** — content-addressed by-products |

**The calculation cache is the strongest piece of the substrate** and should be reused, never
replaced. `calc/store.py` addresses a result by `CalculationKey.build(calc_type, calc_version,
inputs, params)`, hashed through `chemclaw.ids.stable_hash`; `cached_compute` / `run_cached`
are the single lookup-before-compute path; `ResultStore` is a Protocol with an in-memory and a
Postgres backend. The calculator's *version* is in the key, so a method or binary upgrade is a
miss rather than a stale hit.

**The knowledge graph** is markdown + YAML frontmatter in git (D-004), indexed into a NetworkX
`DiGraph` behind a stat-fingerprint cache. `kg/note.py::Note` carries `id, type,
compound_smiles, tags, created_by, source, confidence, valid_from, valid_to, body`; ten
`KNOWN_NOTE_TYPES` are enforced at `kg-validate` rather than at the schema, deliberately, so
the agent can still propose a new one and a human sees it at the PR-gate.

**Retrieval** is four retrievers — graph substring, DRFP Tanimoto, dense vector, lexical FTS —
fused by Reciprocal Rank Fusion (`report/hybrid.py`), with per-retriever weights that ship
inert.

**Data sources** are the `DataSource` seam: a name plus an optional ingest half and an optional
retrieve half. Registered: `graph`, `vector`, `lexical`, `eln-json`, `eln-ord`. D-089 fixed the
scope — **no external sources at all** — and `tests/test_no_egress.py` enforces it because the
prose form of the same constraint demonstrably did not.

---

## 2. Gap findings

| ID | Capability | Current state | What's missing | Why it matters | Severity | Effort |
|---|---|---|---|---|---|---|
| STO-1 | Expensive by-products | **Addressed (D-124/D-132)** — content-addressed artifact store, now on the read path too | nothing (see below: the optimizer and CREST have no by-product worth keeping) | A 76-atom Hessian costs 26 s (binary) / 218 s (finite difference) and was deleted every time | High | M |
| STO-2 | Reuse of a stored Hessian | **Addressed (D-132)** — `HessianSpec` split out of `ThermoSpec`; the matrix is an artifact, the row holds its address | nothing | The highest-value item in this audit: a second temperature now recomputes partition functions, not second derivatives | High | M |
| STO-3 | Conformer-ensemble reuse | **Addressed (D-132)** — `max_members` left the cache key via `unkeyed_fields()`; truncation happens at read | nothing | "Show me 20 instead of 10" is a cache hit rather than a second CREST search | High | S |
| STO-4 | Cross-method geometry reuse | **Addressed (D-132)** — `calc/geometry.py`, a subject-keyed pointer that is itself a cached calculation | callers opting into `starting_geometry` | A converged GFN-FF minimum can seed a GFN2 run; consulting the pointer provably cannot change a cache key | Med | M |
| STO-5 | Converged electronic structure | **Contract only** — no density/orbital set is stored | a media type and link role for DFT restart files | Published measurement: reusing a converged density cut mean SCF iterations ~33 → ~2. Deferred with DFT (D-010) | Med | L |
| STO-6 | Cache cost policy | **Addressed (D-124/D-132)** — `compute_seconds` recorded, and `workflows/artifact_eviction.py` consumes it | nothing | Evicts blobs ordered by cost/idle time and never touches `calculation_results`, so D-011 and `retention.py`'s refusal both stay literally true | Med | S |
| STO-7 | Calc ↔ graph crosslink | **Addressed (D-133)** — multi-file `NoteSubmission`; `calc_refs`/`artifact_refs`; `kg/crosslink.py` reverse lookup | nothing | A job result now wikilinks its compound and both land in one PR; the two halves of the system's memory can cite each other | High | M |
| STO-8 | Typed edges | **Addressed (D-134)** — `[[rel:target]]` plus a frontmatter `relations:` list, vocabulary from RXNO/CHMO/CHEMINF/OntoRXN | nothing | `related(graph, id, "precursor-of")` is a query that can be asked | High | M |
| STO-9 | Edge-level bi-temporality | **Addressed (D-134)** — `valid_from`/`valid_to` on a `Relation`, honoured by `related(..., as_of=)` | nothing | A relation that stopped holding is expressible without deleting it | Med | S |
| STO-10 | Seed corpus | **Addressed (D-135)** — 37 notes covering every type and every relation, plus the awkward cases | growth from real use | `make kg-validate` validates something; retrieval and conflict properties are measurable on real content | Med | M |
| STO-11 | Semantic retrieval default | **Open by design** — `embedding_provider` defaults to `hash`, a feature-hash stand-in | nothing in code; a deployment must point it at a real model | Token-overlap cosine is not neural-semantic retrieval, and the default is silent about it | Low | — |
| STO-12 | Tool-result caching | **Addressed (D-135), largely as a non-gap** — `embed_texts` cached, keyed on provider+model+dim | nothing | The chem tools are correctly left alone: a Postgres round trip is slower than the RDKit call it would cache | Low | S |
| STO-13 | Audit-trail disposal | **Open, correctly refused** | archive-then-reseal with an out-of-band genesis anchor | Deleting from a hash chain is indistinguishable from the tampering it detects; needs an ADR with QA sign-off, not a cleanup job | Med | L |
| STO-14 | Vendored reference data | **Addressed (D-135)** — `sources/vendored/` behind the manifest seam, checksummed and licence-labelled | a third-party dataset, which is a build step plus a licence review | Raises the ceiling `chemclaw/reagents.py` puts on `resolve_compound`; `tests/test_no_egress.py` extended, never relaxed | Med | M |

---

## 3. Executive summary — the five that matter

- **Everything expensive was deleted, and the cheap thing was kept (STO-1/2).** The system
  cached a JSON summary and destroyed the Hessian that produced it. Worse, the cache key for
  thermochemistry includes the temperature, so the one question a stored Hessian trivially
  answers — the same molecule at a different temperature — was the exact question that forced a
  full recomputation. D-124 keeps the bytes; splitting the spec is what makes keeping them pay.

- **Computed results are graph islands (STO-7).** `connectors/qm/knowledge.py` documents the
  blocker precisely: it cannot wikilink a compound note that may not exist, because the dangling
  link would fail validation on the very PR it opens. The consequence is that the calculation
  store and the knowledge graph, the two halves of the system's memory, cannot reference each
  other in either direction. The fix is not a reverse index — it is letting the PR-gate submit a
  note *with its dependencies*, which `memory/supersede.py` already wanted and worked around by
  naming a replacement in plain text instead of a link.

- **The graph has no relations, only links (STO-8/9).** Every edge is an untyped wikilink. A
  knowledge graph in which nothing can be said *about* a connection is a citation network, and
  the retrieval layer treats it as one. The syntax is free to take: `_SLUG` excludes `:`, so
  `[[precursor-of:compound-x]]` currently parses as a dangling link and fails `kg-validate` —
  nobody can already be relying on it.

- **The graph is empty (STO-10).** `knowledge/` contains `.gitkeep`. `make kg-validate` passes
  because there is nothing to validate. Every retrieval, conflict and crosslink property is
  measured against `evals/retrieval_corpus/` fixtures — which is correct for pinned eval numbers
  and no substitute for a real corpus. This is also why STO-8's syntax claim could be verified
  by inspection: there is no content to break.

- **"Tool result caching" was mostly a non-gap, and saying so is the finding (STO-12).** Every
  tool in the `calc` connector already goes through `run_cached`; the `chem` tools are RDKit
  calls that a Postgres round trip would make *slower*. Building a caching subsystem here would
  have been pure ceremony. The one genuine repetition is `embed_texts`, which re-embeds the same
  query on every retrieval — a network round trip per query under the real provider.

*Deferred by design, correctly, and not counted as defects:* DFT wavefunction storage (STO-5,
gated on D-010), audit-trail disposal (STO-13, needs QA sign-off), and the `hash` embedding
default (STO-11 — it is the offline dev path, and it says so in its own docstring).

---

## 4. External practice this borrows from

- **QCArchive / QCFractal** — the reference model for a QM result database. Its scalar records
  are ~2 kB (≈500 M computations per TB) while wavefunctions are not: summary and artifact are
  *separate storage tiers*, which is exactly the split D-124 adopts.
- **AiiDA** — a provenance DAG over inputs, codes and outputs, and the FAIR-for-workflows
  argument. The model for STO-7's lineage: a result should carry enough to be reproduced years
  later.
- **DataJoint 2.0** — hash-addressed versus schema-addressed blob storage for scientific
  objects, with chunked containers (Zarr/HDF5) for array data. A Hessian is precisely this
  shape; the hash-addressed table plus link table is taken verbatim.
- **ORCA `MORead`/`AutoStart`, Gaussian `Guess=Restart`** — the measured case for STO-5.
- **Graphiti / Zep** — bi-temporal edges with fact *invalidation* rather than deletion. The
  system already does this on nodes (`valid_from`/`valid_to`, `is_current`); STO-9 is the
  missing half.
- **RXNO, CHMO, CHEMINF, OntoRXN** — established typed-relation vocabularies for chemical
  knowledge graphs, so STO-8's relation set is adopted rather than invented.

---

## 5. Recommended order

All of it landed, in this order: `STO-1` (D-124) → `STO-2`/`STO-3`/`STO-4` (D-132) → `STO-7`
(D-133) → `STO-8`/`STO-9` + the conflict signal (D-134) → `STO-12`/`STO-14`/`STO-10` (D-135).

`STO-1` was load-bearing and stayed that way: everything in the reuse column depends on the bytes
being kept, and `artifact_refs` would mean nothing without them.

**Still open, and deliberately so.** `STO-5` (DFT wavefunctions — contract and media types only,
gated on D-010), `STO-11` (the `hash` embedding default, which is the offline dev path and says so),
and `STO-13` (audit-trail disposal, which needs archive-then-reseal with QA sign-off, not a cleanup
job). Two things the vendored-data work leaves for a later pull request: a genuine third-party
dataset, and the DEP-1 question of how `knowledge_dir` is populated in a cluster — see below.


---

## 6. What the implementation changed about the audit itself

Three findings only appeared once the code was written, and they are recorded here rather than
quietly absorbed.

**Not every task has a by-product worth keeping.** The audit assumed the optimizer and CREST paths
merely needed capture wiring. They do not: `xtbopt.xyz` is parsed in full into
`OptimizationResult.structure` and CREST's ensemble file into `ConformerEnsemble.conformers`, both
of which the result cache already persists. Capturing them would be a second copy of the cache.
`_ALREADY_STORED` in `calc/xtb_cli.py` names them, and an `opt` run now captures nothing. The
Turbomole `hessian`/`vibspectrum` are the genuine exception and are kept alongside the `.npy`,
because the two serve different readers.

**A cached row is not a cache hit when its artifact is gone.** This was not in the audit at all,
and it is the detail that keeps the whole design honest — without it the eviction sweep STO-6 asked
for would be data loss rather than a reclaim. See D-132.

**The seed corpus must not absorb the eval corpus.** The plan proposed promoting
`evals/retrieval_corpus/` into `knowledge/`. That directory's own README explains why not: keeping
the gold corpus outside `knowledge_dir` is what makes recall/precision reproducible and independent
of the live graph. The seed corpus was written alongside it instead, and a test asserts they share
no ids.

## 7. How `knowledge_dir` is populated in a cluster — and what the seed corpus changes

**Correction.** An earlier revision of this section said this was "assessed rather than solved" and
that "nothing in this repository does that pull today". That was wrong, and it was wrong because it
was reasoned from `settings.knowledge_path` and `kg/git_submitter.py` without reading `deploy/`.
DEP-1 and DEP-2 were closed in Wave 0 (`BACKLOG.md`, "Done — W0 deployment truth"), and the shape
they took is the one this section went on to recommend.

**What actually exists.** `deploy/knowledge-sync.sh` runs in three modes off one clone-or-refresh
core: `once` as an init container (so a pod never serves traffic against an empty graph), `loop` as
a refresh sidecar on `knowledge.sync.intervalSeconds` (so a merged note reaches a live pod without
a redeploy), and `checkout` to provision the background worker's separate *writable* clone for the
PR-gate submitter. The refresh is `fetch` + `reset --hard`, never `pull`, because a read replica
must not be able to land on a merge conflict; the submitter's clone is a different directory,
because `git checkout -B` switches a whole working tree. `tests/test_deploy_chart.py` gates both.

**What the seed corpus adds, which is new and worth stating plainly.** The published tree is
written with `rsync -a --delete`, so the two configurations differ in a way that is silent:

| `knowledge.sync.repoUrl` | What a pod serves |
|---|---|
| empty (default) | the corpus baked into the image — now the 37 seed notes rather than an empty directory |
| set | the remote branch's `knowledge/` subtree, which **replaces** the shipped corpus on the first sync |

So a deployment that points `repoUrl` at its own knowledge repository loses the seed notes unless
it commits them there. That is the right default — the remote is the source of truth, and a pod
silently merging image content into a curated corpus would be worse — but it is the kind of thing
that is obvious only after it happens. It is now recorded in `knowledge/README.md` as well.

The seed corpus also makes the empty-`repoUrl` path genuinely useful for the first time: a
dev or demo deployment now gets a graph with real shape instead of one that validates because it
contains nothing.
