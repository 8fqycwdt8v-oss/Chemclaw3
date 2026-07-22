# 09 — Knowledge Management & Retrieval: Completeness Gap Analysis

**Scope:** the knowledge stores and retrieval paths of Chemclaw3 (KG/NetworkX, ELN ingestion, memory synthesis, fingerprint search, report harness). Completeness/coverage only — correctness/security bugs (prior audit, Phases 3–7, D-053) are out of scope.
**Date:** 2026-07-22

Chemclaw3 keeps knowledge in four shapes, deliberately without a vector store (D-004). (1) A **Git-backed Markdown knowledge graph**: each note is YAML front-matter plus a body whose `[[wikilinks]]` are edges, indexed on demand by a NetworkX builder (`kg/graph.py`); agent-authored notes enter only via a **PR-gate** (`kg/pr_gate.py`, D-005). (2) **ELN ingestion** maps a source's own format into one canonical `OrdReaction` (`eln/ord.py`) behind an adapter seam (`eln/adapter.py`), with a JSON adapter and a native-ORD adapter; a cursor-driven sync (`eln/sync.py`, `eln/cursor.py`) validates, fingerprint-indexes, and PR-gates each reaction. (3) **Memory synthesis** (`memory/`) turns ingested reactions into `campaign`/`optimization-campaign`/`playbook`/`interaction` notes — again through the one PR-gate. (4) **ECFP4/DRFP fingerprint search** over Postgres+pgvector (`mcp_servers/fpstore.py`, `molfp/`, `rxnfp/`) is the structural index. Retrieval is exposed to the agent as `find_notes`/`expand_note` (graph), `find_similar_*`/`substructure` (structure), and `gather_evidence` (`agents/research_tools.py`), which fans a query across every `SourceRetriever` (`report/retrievers.py`). The corpus today is modest and human-curated; that context governs every severity call below.

---

## 1. Ingestion pipeline — consistent repeatable source→indexed-knowledge?

**Verdict:** Present & wired end-to-end.

**Evidence:** `eln/sync.py:59-97` is a single deterministic loop: `adapter.fetch_new_entries(since)` → `adapter.map_to_ord` → `ingest_reaction`. `eln/ingest.py:27-46` does the fixed sequence — `validate_ord` (refuse invalid), fingerprint-index reaction (DRFP) + each compound (ECFP4), then PR-gate a `reaction` note. Cursor state is durable and incremental (`eln/cursor.py:27-45`, `sync_cursors` table). Adapters do real parse/metadata/segmentation, not hand assembly: `eln/json_adapter.py:149-166` maps structured fields and recovers headline conditions from prose, and `_segment_steps` (`:169-185`) chunks the procedure into ordered `ReactionStep`s. A bad entry is rejected-and-logged, never aborting the batch (`sync.py:76-84`).

**Assessment:** This is a genuine, repeatable pipeline with parse + chunk (steps) + metadata (roles/amounts/conditions/provenance/project), idempotent by construction (id-keyed upserts + idempotent note branch). Exactly what a traceable GxP ingest needs.

**Gap severity:** None.

---

## 2. Structural consistency — front-matter/metadata/linking uniform across docs?

**Verdict:** Present & good.

**Evidence:** One schema, one parser, one renderer: `kg/note.py:34-88` (`Note` model, `_slug` id/type validation, `outgoing_links`), `read_note` (`:95-114`) is the only parser, `kg/render.py:13-21` the only serializer, and it round-trips (`parse_note(write(render_note(n))) == n`). `kg/validate.py:16-41` enforces the three corruption modes (unparseable/invalid, duplicate id, dangling link) and runs as a CI gate (`:44-57`). Every write path — ELN notes (`eln/note.py`), all four memory note builders, report notes (`report/harness.py:125-147`) — constructs a `Note`, so nothing bypasses the schema.

**Assessment:** Author variance is structurally impossible: notes are model-validated on read and machine-rendered on write, and `kg-validate` gates the PR. This is stronger than a typical Markdown KB.

**Gap severity:** None.

---

## 3. Retrieval mechanism — wired query→answer, or naive scan with a deliberate mechanism sitting unused?

**Verdict:** Present & wired — but the *entry* step is a literal substring scan, and graph traversal is only the expansion.

**Evidence:** `agents/graph_tools.py:44-65` `find_notes` builds the whole graph and does `needle in haystack` substring match over id/type/smiles/tags/body of **every** node. `expand_note` (`:68-96`) then does the deliberate graph traversal (`kg.graph.neighborhood`, 1–2 hops, hop-clamped). `report/retrievers.py:41-72` `GraphRetriever.retrieve` is the same substring candidate filter. There is no separate, better mechanism left unused — traversal and substring are both live and complementary (substring finds the anchor, traversal expands it), which is exactly the D-004 design (`DECISIONS.md:20-24`).

**Assessment:** Nothing falls back to a scan while a real index rots unused — the concern that motivates this capability does not apply here. The substring anchor-finder is coarse (feeds items 4 and 5), but query→answer is genuinely connected end-to-end and every hit is a real, citable note.

**Gap severity:** Low.

---

## 4. Query understanding — rewriting/expansion/intent, or purely literal?

**Verdict:** Absent in code (by design — pushed to the LLM skill layer).

**Evidence:** All text retrieval is literal case-folded substring: `graph_tools.py:57-64`, `report/retrievers.py:62-64` (`needle = query.lower(); if needle in haystack`). No stemming, synonym expansion, spelling normalization, or intent classification anywhere in `kg/`, `report/`, or `agents/*_tools.py`. Query decomposition and anchor selection are explicitly the `deep-research`/`knowledge-graph-query` **skills'** job (`agents/research_tools.py:12-16`, `search_tools.py:9-13`).

**Assessment:** For a chemistry KB the literal layer is brittle: `find_notes("reflux")` misses "heated to boiling"; substring `ester` matches `polyester` (the code even admits this, `retrievers.py:47-49`). The mitigation — the agent rewrites/expands the query and picks structural anchors (SMILES) before calling the tool — is real and appropriate for a small corpus, but it is unmeasured (see item 13) and invisible to any non-agent caller. Not a code defect; a deliberate division of labor with a soft edge.

**Gap severity:** Medium — one-line failure: a chemist's synonym or inflected term silently returns nothing from the graph unless the LLM happens to reformulate it, and there is no test proving it does.

---

## 5. Ranking / reranking — relevance ranking, or store default order?

**Verdict:** Partial — structural search is ranked; text retrieval is unranked and then blind-truncated.

**Evidence:** Fingerprint search ranks by Tanimoto descending (`mcp_servers/fpstore.py:124-125`, SQL `ORDER BY … , id`). But graph text hits are appended in note-iteration (disk) order with **no** score (`graph_tools.py:58-65`, `retrievers.py:58-71`). `gather_evidence` concatenates all retrievers' chunks, exact-dedups, and caps at `settings.gather_evidence_max_chunks` with no relevance sort (`research_tools.py:70-91`) — so truncation is arbitrary. The `Note.confidence` field (`note.py:72`) that could weight results is **never read** in any retrieval path (confirmed: no `.confidence` reference outside the model/tests).

**Assessment:** For structural precedent, ranking is correct. For prose evidence, "first N notes off disk that contain the substring" is not relevance order, and the cap can drop the most pertinent note. At today's corpus size the cap is rarely hit; as the KB grows this becomes a silent recall problem. The unused `confidence` field is a designed-but-unwired lever.

**Gap severity:** Medium — one-line failure: a broad `gather_evidence` sweep returns the cap's worth of incidental substring hits and truncates away the one directly-relevant campaign note, with no signal that it did.

---

## 6. Provenance & citation — source/version/date/author on retrieved content?

**Verdict:** Present & strong for citation; Partial for rich provenance surfaced at read time.

**Evidence:** `EvidenceChunk` **mandates** `source_note_id` + `retriever` (`report/evidence.py:16-23`); the harness refuses uncited synthesis and `verify_claims` drops any claim citing a note not actually retrieved (`report/harness.py:100-122`). Notes carry `created_by`, `source`, `confidence`, `valid_from/to` (`note.py:68-74`) and Git supplies version/date/author. Retrieved content is envelope-framed with its note id (`agents/framing.py:20-27`). **However**, the read-time view `NoteRef` exposes only `id/type/compound_smiles/tags` (`graph_tools.py:23-30`) — not `source`, `created_by`, date, or `confidence` — and `valid_from/to` are never surfaced.

**Assessment:** Every answer can be traced to a note, and the note is in Git with full history — the GxP "trace to origin" bar is met. What is not carried *into the answer* is the finer provenance (who authored it, when, human-vs-agent, confidence) that would let the agent weigh sources without a second lookup. Recoverable via `git log`/`expand_note`, so this is a convenience/latency gap, not a traceability hole.

**Gap severity:** Low.

---

## 7. Freshness & staleness — detect changed source & re-index/invalidate?

**Verdict:** Partial — graph reads are always live; the fingerprint copy and time-validity are not invalidated.

**Evidence:** Graph retrieval rebuilds from disk on **every** call (`graph_tools.py:55,81`; `retrievers.py:58` `load_notes`), so an edited/merged note is instantly current — there is no stale cache to invalidate. ELN sync is incremental by high-water cursor (`cursor.py`). **But:** the fingerprint index is a separate serving copy written once at ingest (`ingest.py:42-44`); nothing re-indexes or removes an entry when its note is later edited or deleted — the index can diverge from the graph. And `valid_to` (an expiry date, `note.py:74`) is **never checked at read**, so a note past its validity still serves as current fact with no signal.

**Assessment:** The graph is refreshingly stale-proof (rebuild-per-query — see the cost side in item 14). The two real freshness gaps are (a) fingerprint/graph drift on note mutation and (b) unenforced time-validity. For a GxP base that must not present superseded conditions as current, unenforced `valid_to` is the sharper one, though the small human-reviewed corpus and Git history bound the blast radius.

**Gap severity:** Medium — one-line failure: a superseded reaction note whose `valid_to` has passed is retrieved and cited as current guidance, indistinguishable from a live one.

---

## 8. Conflict handling — two sources disagree: resolve or silent?

**Verdict:** Absent.

**Evidence:** Retrieval unions and returns everything matching; there is no recency/authority/agreement arbitration anywhere in `graph_tools.py`, `retrievers.py`, or `research_tools.py`. `gather_evidence` dedups only *identical* (id, content) pairs (`research_tools.py:77-83`). The fields that could arbitrate — `confidence`, `valid_from/to` — are unread (items 5, 7). Two notes asserting different yields for the same transformation are both surfaced, unflagged.

**Assessment:** Real for a knowledge base that must be *correct*, but heavily mitigated here: the PR-gate puts a human on every entry, the corpus is small, and the `deep-research` skill is instructed to keep evidenced fact separate and let the model weigh sources. So conflicts are surfaced (both notes returned) rather than one being silently hidden — the failure mode is "no help resolving", not "silently wrong". Still, no *signal* that a conflict exists.

**Gap severity:** Medium — one-line failure: two campaign notes report contradictory optimal temperatures and the agent, seeing both as equal evidence, averages or arbitrarily picks one with no contradiction flag.

---

## 9. Access control on retrieval — respects doc/source permissions per caller?

**Verdict:** Absent — deferred by design (broad internal read).

**Evidence:** No identity/authz check in any retrieval tool (confirmed: no `authorize`/`actor`/`identity` reference in `graph_tools.py`, `search_tools.py`, `research_tools.py`, `retrievers.py`). The authorization gate guards only *expensive triggers*, not reads (`agents/authz.py:22-42`); `skill_access.py` gates skill *visibility*, not document content. The Postgres-RLS graph mirror and per-project confidentiality are explicitly deferred (`DEFERRED.md:9`, `:61-63`): "Broad internal read access is fine for cross-project learning… Trigger: real, combinatorial project-level confidentiality requirements."

**Assessment:** This is a conscious, ADR-backed deferral with a stated trigger, not an oversight — and the whole memory/playbook design *depends* on cross-project reads. It only becomes a gap if confidential and non-confidential projects must coexist in one graph, which is not today's requirement. Flag it as a known frontier, not a defect.

**Gap severity:** Low (deferred by design; would rise to High the moment project-level confidentiality is a real requirement).

---

## 10. Deduplication — near-duplicate noise; any dedup?

**Verdict:** Partial — exact/id dedup only, no near-duplicate detection.

**Evidence:** Ingestion is id-idempotent (`ingest.py` docstring; upserts). Synthesized notes get content-derived stable ids so re-runs propose the *same* note, not a duplicate (`memory/ids.py:12-19`). `kg-validate` rejects duplicate ids (`validate.py:30-31`). `gather_evidence` dedups exact `(source_note_id, content)` tuples (`research_tools.py:77-83`). There is **no** near-duplicate/semantic-dup detection (two differently-worded notes about the same experiment both surface).

**Assessment:** For a Git-curated corpus where a human reviews every merge and synthesized ids are deterministic, exact dedup is the right amount — near-dup clustering would be over-engineering against the current KISS/Rule-of-Three discipline. Fine as-is.

**Gap severity:** Low.

---

## 11. Multi-modal / structured knowledge — structured chemistry represented & retrievable, or prose-only?

**Verdict:** Partial — structured *reaction/molecule* data is first-class; analytical modalities are dropped (deferred).

**Evidence:** Structured chemistry is richly modeled and retrievable: `OrdReaction` carries structures, roles, amounts, conditions, ordered `steps` (`eln/ord.py:79-142`), fingerprint-indexed as ECFP4/DRFP for similarity + substructure search (`molfp/`, `rxnfp/`), and rendered as comparative **tables** in optimization notes (`memory/optimization.py:58-72`). But spectra/chromatograms/images have no schema field and are explicitly deferred — `DEFERRED.md:23` "Multimodal analytical data (spectra/images)… When analytical raw data beyond structured fields is needed", and `:30` compound notes deferred.

**Assessment:** The structured content most central to reaction R&D (SMILES, reactions, conditions, side-by-side condition tables) is represented and searchable — not silently dropped. What *is* dropped is raw analytical evidence (NMR/HPLC), which for GxP is eventually load-bearing proof of identity/purity. That is a genuine future gap but an ADR'd deferral with a clear trigger, not a latent bug.

**Gap severity:** Medium (deferred by design; matters when analytical raw data enters scope — then a note citing "85% yield" has no linkable spectrum to substantiate it).

---

## 12. Feedback loop — flag bad retrieval / wrong entry that feeds a correction?

**Verdict:** Partial — a positive correction path exists; no negative "this retrieval/entry was wrong" signal.

**Evidence:** The `interaction` memory layer captures a chemist's *confirmed or corrected* answer as a new PR-gated note (`memory/interaction.py:13-52`, `agents/interaction_tools.py`), and Git PR review can edit/reject any note — so corrections *can* enter. But there is **no** mechanism to flag a specific retrieval as bad, or mark an existing note as wrong/superseded so it is down-ranked or excluded: a wrong note simply persists until a human manually edits/deletes it in Git, and `valid_to` (the natural tombstone) is unenforced at read (item 7).

**Assessment:** Corrections re-enter as *new* knowledge, but the KB is effectively write-plus-manual-edit with no in-band "demote this" signal and no closed loop from a bad answer back to the offending entry. For a small, human-reviewed corpus this is livable; it will not scale as a self-improving retrieval system.

**Gap severity:** Medium — one-line failure: the agent surfaces a note a chemist knows is wrong; the chemist can only fix it by hand-editing Git, and nothing prevents the same note being re-surfaced meanwhile.

---

## 13. Retrieval evaluation — query set with known-correct sources measuring precision/recall?

**Verdict:** Absent.

**Evidence:** The eval layer scores *scientific output* only — `e_factor`, `pmi`, `prediction_error`, `bo_regret` (`evals/metrics.py:64-167`) — and the four case files are all mass-balance/prediction/BO cases (`evals/cases/`). There is **no** retrieval metric registered and **no** case shaped as "query → expected source note ids", so precision/recall/MRR of `find_notes`/`gather_evidence` is never measured. The `@metric` registry (`evals/metric.py:72-94`) is extensible enough to add one, but none exists.

**Assessment:** This is the most consequential *measurement* gap. The system's core value proposition is "surface the right evidence", yet its retrieval quality — and the item-4 claim that the LLM compensates for literal matching — is entirely anecdotal. Without a gold query set, retrieval regressions (a schema change, a cap tweak, a synonym miss) are invisible and cannot gate a PR the way scientific metrics do. Corpus is small, but that is exactly when a gold set is cheap to build.

**Gap severity:** High — one-line failure: a change to the substring filter or the `gather_evidence` cap quietly halves recall on real questions and no test, metric, or gate catches it.

---

## 14. Scale behavior — holds at 10×, or depends on staying small?

**Verdict:** Partial — correct and documented at current scale; two known super-linear paths with identified escape hatches.

**Evidence:** Every graph retrieval rebuilds the entire graph from disk — full parse + substring scan of **all** notes — on each `find_notes`/`expand_note`/`GraphRetriever.retrieve` call (`graph_tools.py:55,81`, `retrievers.py:58`); there is no persistent graph index, so cost is O(N notes) *per query*. Memory clustering is O(n²) pairwise Tanimoto (`memory/similarity.py:44-49`, `playbook.py:44-47`). Both are consciously deferred with triggers: `DEFERRED.md:25` and `:64` (sub-quadratic clustering at ~10⁴ reactions → Postgres HNSW k-NN). Fingerprint *search* already uses HNSW (`fpstore.py:128-138`).

**Assessment:** At today's modest corpus this is fine and the docstrings are honest about it. The per-query full-graph rebuild is the sharper concern because it is paid on *every retrieval*, not just periodic synthesis — a 10×–100× graph makes interactive retrieval slow well before the O(n²) synthesis job (a background task) hurts. The fix (persist/cache the index, invalidate on merge) is known and modest. Deferred-by-design, but the retrieval-path rebuild is worth pulling forward earlier than the clustering.

**Gap severity:** Medium — one-line failure: at 10× notes, each `gather_evidence` call re-parses the whole `knowledge/` tree from disk, turning interactive Q&A latency from tens of ms into seconds.

---

## Gap findings table

| ID | Capability | Current State | What's Missing | Why It Matters For This System | Gap Severity | Effort |
|----|------------|---------------|----------------|-------------------------------|-------------|--------|
| KM-1 | Ingestion pipeline | Present & wired (adapter→validate→index→PR-gate, cursor-driven, idempotent) | — | Repeatable, traceable ingest is the backbone; it is met | None | — |
| KM-2 | Structural consistency | Present & good (one schema/parser/renderer, `kg-validate` CI gate) | — | Uniform front-matter/linking is enforced, not hoped for | None | — |
| KM-3 | Retrieval mechanism | Present & wired (substring anchor + graph traversal, both live) | — (coarse anchor feeds KM-4/5) | Query→answer is genuinely connected; no unused-index problem | Low | — |
| KM-4 | Query understanding | Absent in code; literal substring, understanding pushed to LLM skill | Stemming/synonym/expansion/intent, or a test proving the LLM covers it | Chemistry synonyms/inflections silently miss at the lexical layer | Medium | M |
| KM-5 | Ranking/reranking | Partial: structural ranked; text unranked then blind-capped; `confidence` unused | Relevance scoring for text hits; rank-before-truncate; wire `confidence` | Broad sweep can truncate away the most relevant note unsignalled | Medium | M |
| KM-6 | Provenance & citation | Present & strong for citation; rich provenance not surfaced at read | Carry source/author/date/confidence into `NoteRef`/chunks | Trace-to-origin is met; weighing sources needs a second lookup | Low | S |
| KM-7 | Freshness & staleness | Partial: graph always live; fp index & `valid_to` not invalidated/enforced | Re-index fp on note mutation; enforce `valid_to` at read | Superseded/expired conditions can serve as current fact | Medium | M |
| KM-8 | Conflict handling | Absent (both disagreeing notes returned, no flag) | Recency/authority/agreement signal on conflicting notes | Contradictory conditions surface with no contradiction signal | Medium | M |
| KM-9 | Access control on retrieval | Absent — deferred by design (`DEFERRED.md:9`) | Per-caller doc/project scoping (RLS mirror) | Only matters when confidential+open projects share the graph | Low | L |
| KM-10 | Deduplication | Partial: exact/id dedup only | Near-duplicate detection | Right amount for a Git-curated corpus; over-eng to add now | Low | — |
| KM-11 | Multi-modal / structured | Partial: reactions/molecules/tables first-class; analytical dropped (`DEFERRED.md:23`) | Spectra/chromatogram/image ingestion & linking | Structured chemistry retrievable; raw analytical proof deferred | Medium | L |
| KM-12 | Feedback loop | Partial: positive correction (interaction notes) only | In-band "flag bad retrieval / demote wrong entry" signal | Wrong notes persist until manual Git edit; no closed loop | Medium | M |
| KM-13 | Retrieval evaluation | Absent (only scientific-output metrics exist) | Gold query→expected-source set + a registered retrieval metric | Retrieval regressions are invisible and ungated | High | M |
| KM-14 | Scale behavior | Partial: full-graph rebuild per query (O(N)), O(n²) clustering; both deferred with triggers | Persistent/cached graph index invalidated on merge | Per-query re-parse makes interactive Q&A slow at 10× | Medium | M |

---

## Executive summary — the five most important knowledge-management gaps

- **No retrieval evaluation (KM-13, High).** The system's core promise is "surface the right evidence", yet only scientific-output metrics exist (`evals/metrics.py`) — there is no query→expected-source gold set and no retrieval metric. Retrieval quality is anecdotal and ungated, so any regression (a filter change, a cap tweak, a synonym miss) is invisible. Cheapest high-value fix, and a small corpus is the ideal time to build the gold set.

- **Unranked, blind-truncated text retrieval (KM-5, Medium).** Structural search ranks by Tanimoto, but graph/text hits come back in disk order and `gather_evidence` caps at a fixed budget with no relevance sort (`research_tools.py:70-91`) — the most relevant note can be truncated away. The `Note.confidence` field that would help is designed but never read.

- **No staleness enforcement, conflict signal, or negative feedback (KM-7/8/12, Medium cluster).** `valid_to` is never checked at read, contradictory notes are both returned with no flag, and there is no way to mark a retrieved entry as wrong short of hand-editing Git. Individually livable at current scale; together they mean a GxP base can serve superseded or conflicting facts with no signal.

- **Retrieval is literal substring matching (KM-4, Medium).** All text retrieval is `needle in haystack` (`graph_tools.py`, `retrievers.py`) — no stemming, synonyms, or expansion; the code itself notes `ester`⊂`polyester`. Query understanding is delegated to the LLM skill, which is reasonable but unproven and invisible to any non-agent caller.

- **Full-graph rebuild on every query (KM-14, Medium).** Each retrieval re-parses the entire `knowledge/` tree from disk (O(N) per call). It is honestly documented and fine now, but because the cost is paid per interactive query — not per background job — it will degrade Q&A latency at 10×–100× sooner than the separately-deferred O(n²) synthesis clustering, and warrants pulling its fix forward.

*Deferred-by-design, correctly (not counted as defects): access control on retrieval (KM-9, `DEFERRED.md:9` — gated on real project confidentiality), analytical multi-modal data (KM-11, `DEFERRED.md:23`), and the O(n²) clustering half of KM-14 (`DEFERRED.md:25`). Each has an ADR and a concrete revisit trigger.*
