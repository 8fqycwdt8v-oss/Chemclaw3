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
| STO-1 | Expensive by-products | **Addressed (D-124)** — content-addressed artifact store; xTB Hessian/vibspectrum/geometry captured before the tempdir dies | wiring for the optimizer and CREST paths; the eviction sweep | A 76-atom Hessian costs 26 s (binary) / 218 s (finite difference) and was deleted every time | High | M |
| STO-2 | Reuse of a stored Hessian | **Open** — `ThermoSpec` puts `temperature_k`, `pressure_pa`, `symmetry_number`, `rrho_cutoff_cm` in the cache key, so thermochemistry at a second temperature recomputes the Hessian | split `HessianSpec` from the RRHO block that reads it | The single highest-value item in this audit: recomputing a temperature-independent quantity to answer a temperature question | High | M |
| STO-3 | Conformer-ensemble reuse | **Open** — `ConformerSpec` keys on `max_members`, which only truncates a list | drop it from the key; truncate at read | "show me 20 instead of 10" re-runs CREST, the module's own docstring's "most expensive single calculation in the system" | High | S |
| STO-4 | Cross-method geometry reuse | **Open** — keyed on coordinates, so the same SMILES from a different embedding re-optimizes | a subject-keyed geometry pointer (itself a cached calculation, no new table) | A converged GFN-FF minimum cannot seed a GFN2 run | Med | M |
| STO-5 | Converged electronic structure | **Contract only** — no density/orbital set is stored | a media type and link role for DFT restart files | Published measurement: reusing a converged density cut mean SCF iterations ~33 → ~2. Deferred with DFT (D-010) | Med | L |
| STO-6 | Cache cost policy | **Addressed (D-124)** — `compute_seconds` recorded on every miss | the eviction sweep that consumes it | `workflows/retention.py` named the missing policy and correctly refused to fake it with an age cutoff | Med | S |
| STO-7 | Calc ↔ graph crosslink | **Open** — `connectors/qm/knowledge.py` deliberately emits no wikilink, because the compound note may not exist and a dangling link fails `kg-validate` on the PR it opens | `calc_ref`/`artifact_refs` frontmatter; a multi-file `NoteSubmission` so a note and its dependencies land together | Computed results are graph islands: "what we computed" and "what we know" are disjoint stores | High | M |
| STO-8 | Typed edges | **Open** — `kg/graph.py:150` is `graph.add_edge(note.id, target)` with no attributes | a relation in the wikilink (`[[rel:target]]`) plus optional per-edge validity | No relation can say *precursor-of*, *contradicts*, *measured-by*, *computed-from*. Every graph query is structurally blind | High | M |
| STO-9 | Edge-level bi-temporality | **Open** — `valid_from`/`valid_to` exist on nodes only | validity on the edge | A fact that stopped being true is expressible; a *relation* that stopped being true is not | Med | S |
| STO-10 | Seed corpus | **Open** — `knowledge/` holds only `.gitkeep` | ~35 notes covering every type and relation | `make kg-validate` currently validates an empty directory; every retrieval number is measured against fixtures, never real content | Med | M |
| STO-11 | Semantic retrieval default | **Open by design** — `embedding_provider` defaults to `hash`, a feature-hash stand-in | nothing in code; a deployment must point it at a real model | Token-overlap cosine is not neural-semantic retrieval, and the default is silent about it | Low | — |
| STO-12 | Tool-result caching | **Assessed — largely a non-gap** | three memoizations, not a subsystem | Every `connectors/calc` tool already routes through `run_cached`; the chem tools are RDKit calls cheaper than a Postgres round trip. The real win is `embed_texts`, which re-embeds the same query every retrieval | Low | S |
| STO-13 | Audit-trail disposal | **Open, correctly refused** | archive-then-reseal with an out-of-band genesis anchor | Deleting from a hash chain is indistinguishable from the tampering it detects; needs an ADR with QA sign-off, not a cleanup job | Med | L |
| STO-14 | Vendored reference data | **Open** — the one sanctioned escalation of D-089 | a build-time-vendored ontology/reagent corpus behind the existing source seam | `chemclaw/reagents.py` is a hand-maintained name→SMILES table, and that is the ceiling on `resolve_compound` | Med | M |

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

`STO-1` (done) → `STO-2`/`STO-3` (the reuse that pays for it) → `STO-7` (crosslink) → `STO-8`/
`STO-9` (typed edges) → conflict signal → `STO-12` → `STO-14` → `STO-10` (the seed corpus last,
because it is only worth pinning once the shapes it must contain exist).

`STO-1` is load-bearing. Everything in the reuse column depends on the bytes being kept, and the
crosslink's `artifact_refs` field means nothing without them.
