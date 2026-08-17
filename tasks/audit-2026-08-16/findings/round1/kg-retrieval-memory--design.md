# kg / retrieval / memory — design and simplification

Slice: `src/chemclaw/kg/`, `src/chemclaw/retrieval/` (incl. `vectors/`), `src/chemclaw/memory/`.
Lens: structure that costs more than it buys — duplication, single-caller abstraction, dead code,
layering, misleading naming. Every finding below was reproduced by running code; scripts are under
`/tmp/audit/` and their output is quoted verbatim.

---

## The report path fans out over sources a second time, and its copy has no failure isolation

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/retrieval/harness.py:174` (`gather_section`)
  against `/home/user/Chemclaw3/src/chemclaw/retrieval/fanout.py:82` (`_sweep`) /
  `:153` (`sweep_sources`)
- **Trigger**: a report section is retrieved (`durable/report_workflow.py:58 retrieve_section` →
  `gather_section`) while any one of the four production retrievers raises — the vector or lexical
  leg with Postgres down, `ShareDocumentRetriever` on an unreachable share, the fingerprint store
  on a missing index file.
- **Consequence**: `asyncio.gather` without `return_exceptions` propagates the first exception, so
  the whole activity fails, burns its `BAD_DATA_RETRY` budget, and `ReportSectionWorkflow` returns
  `SynthesizedSection(evidence=[], retrieval_failed=True)`. The section renders as
  `_Retrieval failed for this section; incomplete — re-run required._` and the evidence the three
  *healthy* sources did find is discarded. The conversational sweep over the identical retriever
  set degrades per source instead (`fanout._sweep` catches, logs, counts
  `chemclaw_evidence_source_failures_total`, and returns `[]` for that branch only). Two
  implementations of "ask every source the same question", with different error semantics, no
  per-source counter and no stream event on the durable one — which is exactly the blind spot
  `fanout.py`'s own module docstring says the fan-out exists to remove ("a source that returns
  nothing is indistinguishable from a source nobody asked").
- **Evidence**: `harness.py:174-177` is the whole of the second implementation —

  ```python
  gathered = await asyncio.gather(
      *(retriever.retrieve(section.query, section.filters) for retriever in retrievers)
  )
  evidence = [chunk for chunks in gathered for chunk in chunks]
  ```

  `/tmp/audit/repro_gather.py` runs one healthy and one raising retriever through both paths:

  ```
  evidence source 'vector' failed; the sweep continues without it
  gather_evidence sweep  -> [1, 0] chunks per source
  gather_section        -> RAISED RuntimeError: pgvector unreachable
                            -> the whole section is lost, incl. the healthy source's hit
  ```

- **Fix**: delete the second fan-out. `gather_section` calls
  `sweep_sources([(r.name, r) for r in retrievers], section.query, section.filters)` and flattens
  the returned per-source lists (`sweep_sources` already preserves argument order, which is what
  `gather_section`'s docstring says it needs). Behaviour-changing, deliberately: a failing source
  costs its own contribution instead of the section, and the durable path gains the per-source
  counter and stream event the conversational one has. `SynthesizedSection.retrieval_failed` keeps
  its meaning for the case that matters (the activity itself failing), and the ~6 lines of
  concurrency logic in `harness.py` go away.

---

## `GraphRetriever` is the only `SourceRetriever` that returns an unbounded result

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/retrieval/retrievers.py:181-187`
  (`GraphRetriever.retrieve`'s return) versus `:382` and `:417`
  (`VectorRetriever` / `LexicalRetriever`, both `settings.retrieval_top_k`)
- **Trigger**: any section query or `gather_evidence` call whose terms are common in the corpus —
  `"reaction"`, a project tag, a solvent name — on a corpus of realistic size.
- **Consequence**: `GraphRetriever` returns one `EvidenceChunk` per matching note, with no cap. It
  is the *default* and often only enabled source (`data_sources` defaults to `graph`). Every other
  implementation of this contract in the tree bounds itself: `VectorRetriever` and
  `LexicalRetriever` at `retrieval_top_k` (`retrievers.py:382,417`),
  `ingest/eln/warehouse/retriever.py:150` and `ingest/sources/vendored_dataset.py:191` and
  `ingest/documents/retriever.py:192` at the same setting, `FingerprintReactionRetriever` at
  `fingerprint_top_k`. `gather_evidence` survives it because it truncates *after* fusion
  (`research_tools.py:238`), but the report path does not truncate at all — `gather_section`
  concatenates and `report_note` (`harness.py:235`) renders one bullet per chunk into a note that
  is then committed to git and stored verbatim in `note_proposals.content`.
- **Evidence**: `/tmp/audit/repro_cap.py` on the committed corpus grown to 2,039 notes the way a
  real programme grows (many runs of one transformation):

  ```
  corpus notes: 2039
  settings.retrieval_top_k = 8
  GraphRetriever('reaction') returned: 2008 chunks (uncapped)
  report note body: 606446 chars, 8026 bullets
  ```

  and `/tmp/audit/repro_cap2.py` for the conversational cost of chunks that are then thrown away:

  ```
  warm GraphRetriever.retrieve: 23 ms, 2008 chunks, 861 KiB of chunk JSON
  retrieval_top_k (what vector/lexical/warehouse/documents return) = 8
  ```

  A 600 KB single markdown note goes through `pr_gate.propose_note` → `GitNoteSubmitter` → a human
  reviewer, and the same bytes land in the proposal row's `content` column.
- **Fix**: truncate in `GraphRetriever.retrieve` to `settings.retrieval_top_k` after the sort, the
  same line its two siblings in the same file already have. The ranking is already computed
  (coverage, then confidence, then id), so the cut keeps the best hits. Behaviour-changing but in
  the direction every other retriever already took; if the report path wants a wider window than a
  chat turn, that is a second setting, not an absent one. If the intent really is "graph returns
  everything", then `gather_section`/`report_note` need the cap instead — but one of the two has to
  exist, and today neither does.

---

## The typed-edge layer has no production reader; `kg/crosslink.py` has none at all

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/kg/graph.py:280` (`related`),
  `:243-247` (`_assemble_graph`'s per-edge `relations` tuple),
  `/home/user/Chemclaw3/src/chemclaw/kg/note.py:330` (`Relation.confidence`) and
  `:256-311` (`TemporalWindow` as inherited by `Relation`),
  `/home/user/Chemclaw3/src/chemclaw/kg/crosslink.py` (whole module, 61 lines)
- **Trigger**: author a note declaring a typed relation with a confidence and a validity window —
  the exact shape `relations.py` and `memory/failure.py:86` mint — and ask any production consumer
  about it.
- **Consequence**: nothing reads it. Grepping all of `src/`, `skills/`, `.claude/`, `data/` and
  every `*.md`/`*.yaml`/`*.toml` outside `docs/`:
  - `graph.related` is imported by `tests/test_relations.py` and `tests/test_seed_corpus.py` and by
    nothing in `src/`.
  - the edge `relations` attribute is read at exactly one place, `graph.py:296`, inside `related`.
  - `Relation.confidence` is *written* (`memory/failure.py:86`) and read nowhere in `src/`.
  - `Relation.is_current` (the per-edge validity window, the whole reason `TemporalWindow` was
    factored out of `Note`) is called at exactly one place, `graph.py:299`, inside `related`.
  - the only production consumer of `outgoing_relations()` beyond graph assembly is
    `conflicts._declared` (`conflicts.py:130`), which reads `relation.rel` against two names
    (`contradicts`, `supersedes`) and ignores confidence and windows; and `validate.py:95`, which
    checks the name against the vocabulary. The remaining 13 entries of `KNOWN_RELATIONS` are
    validated and never queried.
  - `crosslink.calc_ref_index` / `cited_calculations` / `notes_for_calculation` have no caller in
    `src/` at all — `connectors/qm/knowledge.py:64` and `activities.py:217` only *mention* them in
    comments; the only importers are `tests/test_crosslink.py`, `tests/test_seed_corpus.py`,
    `tests/test_qm_persistence.py`.

  There is no dynamic registration to rescue any of this: there is no `@tool` in `kg/`,
  `retrieval/` or `memory/`, no Temporal workflow discovery here, no entry point, and the only
  non-test mention outside comments is `src/chemclaw/kg/README.md`.

  This is not a purely academic cost. `_assemble_graph` builds `defaultdict(list)` and calls the
  richer `outgoing_relations()` (regex + dedup + `Relation` construction) instead of
  `outgoing_links()`, on every cache-missing graph build, for an attribute nobody reads —
  `/tmp/audit/repro_assemble.py`, 2,038 notes, best of 5:

  ```
  with relations tuple (shipped)  :    22.2 ms   nodes=2038 edges=4059
  links only                      :    11.9 ms   nodes=2038 edges=4059
  ```

  And the semantics are silently inert: `/tmp/audit/repro_relations.py` declares
  `precursor-of → compound-b, confidence 0.9, valid_to 2020-01-01` and shows the window is stored
  and unread —

  ```
  edge attribute stored : (Relation(valid_from=None, valid_to=datetime.date(2020, 1, 1),
                                    rel='precursor-of', to='compound-b', confidence=0.9),)
  related(as_of=today)  : []          # the only reader; no src/ caller
  conflicts sees        : []
  ```

  `expand_note` (`agent/graph_tools.py:176`) reaches neighbours through `neighborhood()`, which is
  undirected and untyped, so `compound-b` is still returned as a current neighbour of `compound-a`
  five years after the edge expired. `NoteRef` carries no relation field at all.

  `kg/README.md` names the first two ("Declared but unwired") and defends keeping them because
  "each is the only read path for a capability a merged ADR claims, and deleting one deletes the
  claim with it". That defence is the shape this repository elsewhere rejects — a function whose
  only caller is its own test is a claim that a capability exists, not the capability — and the
  README understates the footprint: it lists two functions, while what is actually unreached is
  `related` + the edge attribute + `Relation.confidence` + per-edge validity + 13 of 15 relation
  names + the whole `crosslink` module.
- **Fix**: pick one, do not leave it in the middle.
  (a) Wire it: give `expand_note`'s `NoteRef` the relation(s) the edge carries and have it filter
  expired edges through `related`/`Relation.is_current`. Two changed lines in `graph_tools.py`,
  behaviour-changing (a chemist starts seeing why two notes are linked, and stops seeing retired
  links), and it makes every one of the symbols above live.
  (b) Delete it: drop `related`, drop the `relations=` edge attribute from `_assemble_graph`
  (edges become plain, `by_target` goes), drop `Relation`'s inheritance from `TemporalWindow` and
  its `confidence`, and delete `kg/crosslink.py` with its three tests. Behaviour-preserving for
  every production path — the reproductions above are what proves that — and it halves graph
  assembly. `conflicts._declared` and `validate` keep working; they only need `rel` and `to`.
  Doing (a) for the edge query and (b) for `crosslink` is also coherent.

---

## The `reaction-` note-id prefix is spelled eight times, under a docstring claiming one definition

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/kg/note.py:62-71` (`note_id_for_reaction`), and
  the clone sites: `/home/user/Chemclaw3/src/chemclaw/memory/ids.py:19` (`MEMBER_PREFIX`),
  `/home/user/Chemclaw3/src/chemclaw/memory/jobs.py:51`,
  `/home/user/Chemclaw3/src/chemclaw/memory/campaign.py:30`,
  `/home/user/Chemclaw3/src/chemclaw/memory/optimization.py:114`, `:161`, `:192`,
  `/home/user/Chemclaw3/src/chemclaw/memory/observation_mining.py:91`, `:116`.
- **Trigger**: read `note.py:65` — *"One definition, because three callers were each spelling
  `f"reaction-{id}"` themselves and one of them did not."* Then grep for the literal.
- **Consequence**: the claim is false today. `note_id_for_reaction` is used by `retrievers.py`,
  `ingest/eln/note.py`, `ingest/eln/warehouse/retriever.py` and `connectors/rxnfp/server/tools.py`
  — and the entire `memory` package, which mints the citations of every `campaign`, `playbook` and
  `optimization-campaign` note, spells the prefix by hand seven more times. The coupling is not
  cosmetic: `memory/ids.is_cluster_anchored` reads the prefix back off a citation
  (`MEMBER_PREFIX`, a *ninth* spelling) to reconstruct a note id, and two behaviours hang off that
  reconstruction — `playbook.playbook_note` derives the note's `source` from it
  (`playbook.py:113`) and `supersede._is_synthesis_minted` decides whether to retire a merged note
  from it (`supersede.py:96`). Change `note_id_for_reaction` and the retrievers/ingest start citing
  ids the memory layer never writes, while `is_cluster_anchored` silently starts answering `False`
  for every synthesis note — memory notes get stamped with the wrong provenance and the
  supersede pass stops retiring anything, both without an error. That is precisely the failure
  `note_id_for_reaction` was extracted to prevent, reintroduced in the package that produces most
  reaction citations.
- **Evidence**: `grep -rn '"reaction-\|reaction-{' src/ --include=*.py | grep -v note_id_for_reaction`
  returns the eight sites above (plus two unrelated string constants). `note_id_for_reaction` has
  four `src/` callers, none in `memory/`.
- **Fix**: import `note_id_for_reaction` in the six memory modules and replace the f-strings;
  replace `MEMBER_PREFIX` with a single inverse in `kg.note` (`reaction_id_from_note_id`, or keep
  `MEMBER_PREFIX` but derive it as `note_id_for_reaction("")`) so the forward and reverse spellings
  cannot drift. Behaviour-preserving — the rendered ids are byte-identical today; the change is
  that they stay identical after an edit.

---

## `_cosine` exists three times, with two different post-conditions

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/retrieval/vector_index.py:130`,
  `/home/user/Chemclaw3/src/chemclaw/retrieval/vectors/memory.py:18`,
  `/home/user/Chemclaw3/src/chemclaw/ingest/documents/index.py:299`
- **Trigger**: read the three side by side. The two under `ingest/documents` and
  `retrieval/vectors` clamp to `[0, 1]` and each carries a paragraph explaining why (measured:
  "996 of 2000 random normalised vectors, worst 1.0000000000000002"). The one in
  `vector_index.py` — the note index — does not clamp.
- **Consequence**: no live defect, and that is the point: `IndexHit.score` happens to be an
  unbounded `float` and `retrievers._chunks_from_hits:340` happens to clamp again downstream, so
  the missing clamp is invisible. Add a bound to `IndexHit.score` the way `VectorMatch.score`
  (`ge=0.0, le=1.0`) and `DocumentHit.score` already have, and `InMemoryNoteIndex.search_dense`
  raises `ValidationError` on an exact self-match — the exact failure the other two copies'
  docstrings describe having already hit once. Three copies of six lines, two of which know
  something the third does not.
- **Evidence**: `grep -rn "def _cosine" src/` returns exactly those three; only
  `vector_index.py:134` lacks the `min(1.0, max(0.0, ...))`.
- **Fix**: one `cosine_similarity` in `chemclaw.core` (beside `embeddings`, which every one of the
  three already imports from), clamped, with the optional `a_norm` fast path the documents copy
  needs. Three call sites, behaviour-preserving for the two clamped ones and post-condition-fixing
  for the note index.

---

## Checked and found sound (so the absence is reported, not implied)

- **`kg.conflicts` caching.** `_INDEX_LOCK` held across the computation while `graph._CACHE_LOCK`
  is only ever taken inside it — no lock-order cycle exists; `invalidate_cache()` not clearing
  `_INDEX_CACHE` is safe because the index is keyed on the fingerprint `cached_notes` recomputes
  on the way in.
- **`_widest_disagreements`** is the one genuinely intricate function in the slice, and its
  early-stop argument holds: the walk terminates on `gap < threshold`, the self-skip cannot loop,
  and the partner's confidence is carried out of the branch rather than re-derived.
- **In-memory backends** (`InMemoryNoteIndex`, `InMemoryVectorStore`, `InMemoryProposalStore`) are
  reference implementations exercised by the composition tests, not test doubles with no caller.
- **`retrieval/vectors/`** has no consumer inside `retrieval/` — its only `src/` caller is
  `ingest/documents/index.py:1003`. Worth noting as a placement oddity but not reported as a
  finding: the seam is genuinely about dense retrieval and moving it would churn four modules for
  no behaviour.
- **No layering violation found** in the slice: `kg` imports nothing from `agent`/`connectors` at
  module scope (`known_note_types`/`known_relations` lazy-import the registry, declared in
  `tests/test_layering.py::_ALLOWED_LAZY_EDGES`), and `memory`/`retrieval` reach only downward into
  `core`, `kg`, `ingest` and `science`.
