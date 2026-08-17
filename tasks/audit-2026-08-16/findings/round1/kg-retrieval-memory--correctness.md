# kg / retrieval / memory — CORRECTNESS

Slice: `src/chemclaw/kg/`, `src/chemclaw/retrieval/` (incl. `vectors/`), `src/chemclaw/memory/`.
Every file in the slice was read in full. Five findings below, each reproduced with a script that
was actually run; the printed output is quoted verbatim.

---

## `reindex_notes` stamps the post-edit fingerprint onto pre-edit text, so a note edited during the run is never re-embedded again

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/retrieval/vector_index.py:505-528` (`reindex_notes`)
- **Trigger**: any write to a file under `knowledge_path` in the window between `load_notes()`
  returning and `note_file_fingerprints()` scanning. In the shipped topology that window is
  occupied continuously: `deploy/knowledge-sync.sh:189` runs `rsync -a --delete` into exactly
  `${CHEMCLAW_NOTE_REPO_DIR}/${CHEMCLAW_KNOWLEDGE_DIR}` — the directory this reads — every
  `CHEMCLAW_KNOWLEDGE_SYNC_INTERVAL_SECONDS` (default 300), while `durable/note_index.py` runs
  `reindex_notes` hourly *and* on the PR-gate merge webhook. `rsync -a` preserves the source mtime,
  so a delta'd note gets a new `(mtime_ns, size)` — the whole of the fingerprint.
- **Consequence**: the index row for that note stores the **new** fingerprint beside the **old**
  embedded text and the **old** `tsvector`. On every subsequent run `_needs_embedding` compares
  equal and returns False, so the note is *never* re-embedded. The dense and lexical retrieval legs
  answer from the superseded body permanently — silently, with no warning, no metric and no
  self-healing path. The `embedding_key` recovery documented at lines 480-487 does not help
  (the model did not change) and only a manual `--full` clears it.
- **Evidence**: the order of operations is the defect. The fingerprint must be read *before* the
  text it describes, not after:

  ```python
  await asyncio.to_thread(invalidate_cache, directory)
  notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []   # parse
  if not notes:
      return 0
  current_fingerprints = await asyncio.to_thread(note_file_fingerprints, directory)      # stat, later
  ```

  Read before the parse, a concurrent edit leaves the *old* fingerprint stored against new-or-old
  text — which reads as "changed" next run and self-heals. Read after, as here, it leaves the *new*
  fingerprint against old text, which reads as "unchanged" forever. The module docstring
  (lines 496-501) argues at length that the cache bust makes the two halves come "from the same
  moment"; it does not, because the bust only affects the parse, and the second scan is a fresh
  `stat` of a tree that has moved on.

  `/tmp/repro_reindex.py` (patches `vector_index.load_notes` to write the file after returning —
  a faithful stand-in for the rsync landing mid-run):

  ```
  first run embedded: 1
  indexed text  : compound-x compound  OLD BODY about benzene
  indexed fp    : 1786948144715548181:61
  on-disk fp    : 1786948144715548181:61
  on-disk body  : NEW BODY about toluene
  second run embedded: 0
  indexed text after second run: compound-x compound  OLD BODY about benzene
  ```

  Stored fingerprint == current on-disk fingerprint, stored text == the superseded body, and the
  next run embeds nothing.
- **Fix**: read the fingerprints first and diff against those:

  ```python
  await asyncio.to_thread(invalidate_cache, directory)
  current_fingerprints = await asyncio.to_thread(note_file_fingerprints, directory)
  notes = await asyncio.to_thread(load_notes, directory) if directory.exists() else []
  ```

  A note edited during the run then carries a fingerprint older than the file and is re-embedded on
  the next pass. (Belt and braces: re-`stat` each changed note immediately before building its
  `NoteRecord` and store the *minimum* of the two observations — but the reorder alone closes the
  permanent-staleness case, which is the one that matters.)

---

## `mine_corpus` attributes a failure to projects that never recorded one — and that inflated project count is the promotion gate

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/memory/observation_mining.py:65-92` (`mine_corpus`)
- **Trigger**: a similarity cluster whose `FAILURE` runs all belong to one project while a second
  project contributes only an `INCONCLUSIVE` run.
- **Consequence**: `projects` is computed over the *whole* cluster (line 66) while `failures` is
  computed over the FAILURE subset (line 71). The emitted statement then reads
  "One transformation failed in N runs across M projects (…)" with M counting projects that never
  observed a failure. Worse, the `len(projects) < 2: continue` guard on line 68 is what decides
  whether the observation exists at all, and `projects_seen` is what
  `settings.observation_promote_min_projects` counts in `observations._SELECT_PROMOTABLE` — so an
  observation can be created *and* promoted to a PR-gated `playbook` note purely on the strength of
  a run the module's own docstring calls carrying "no evidence about the chemistry".
  `durable/observation_jobs._promotion_summary` copies the statement verbatim into the PR body the
  human signs off on, so the reviewer sees the inflated claim and not what falsifies it — the exact
  failure mode the docstring at lines 47-56 says was fixed for the *success* case and left unfixed
  for the per-project case.
- **Evidence**: `/tmp/repro_mine_corpus.py` — cluster = {a: alpha/FAILURE, b: alpha/FAILURE,
  c: beta/INCONCLUSIVE}:

  ```
  scope    : transformation:a
  projects : ['alpha', 'beta']
  evidence : ['reaction-a', 'reaction-b', 'reaction-c']
  statement: One transformation failed in 2 runs across 2 projects (alpha, beta), with 1 run
             inconclusive (no evidence either way). ...
  ```

  Both failures are in `alpha`. `beta` never failed at this transformation — it never got an
  answer. Without `c` the observation would not have been emitted at all.
- **Fix**: derive the cross-project signal from the failures, not from the cluster:

  ```python
  failures = [m for m in cluster if outcome_of.get(m) is OutcomeClass.FAILURE]
  failing_projects = sorted({p for m in failures if (p := project_of.get(m))})
  if len(failing_projects) < 2:
      continue
  ```

  and use `failing_projects` in both the statement and `projects_seen`. Keep the inconclusive runs
  in `evidence_note_ids` and in the "with N inconclusive" aside, where they already read correctly.

---

## `conflicts_total` is not the total, so the report's "the N strongest of M" caveat silently disappears

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/kg/conflicts.py:97-121` (`_strongest`,
  `NoteConflicts`) and `:181-198` (`_suspected` passing `cap` into `_widest_disagreements`);
  rendered at `/home/user/Chemclaw3/src/chemclaw/retrieval/harness.py:248-261`.
- **Trigger**: more than `conflict_max_per_note` (default 3) notes of one type on one compound
  whose confidences differ by at least `conflict_confidence_gap` (default 0.3).
- **Consequence**: `_suspected` now caps each note's own scan at `cap` pairs, so
  `conflicts_by_note` never sees the full pair set and `NoteConflicts.total = len(ranked)` is a
  count of *surviving* pairs, not of disagreements. When it lands on exactly `cap`,
  `NoteConflicts.truncated` is 0, `harness.report_note`'s `hidden` is 0, and the bullet is rendered
  with **no scope clause at all** — "**Conflicts with r9, r8, r7**" — which is precisely the
  reading `NoteConflicts`' own docstring says must never happen: "A reader who sees three ids and
  no count would reasonably conclude there were three." The claim "every surface that renders the
  ids says '3 of 141' when that is the truth" is false; measured, it says nothing.
- **Evidence**: `/tmp/repro_conflict_total.py`, 10 `reaction` notes on one compound with
  confidences 0.0–0.9 (cap 3, gap 0.3):

  ```
  reaction-r3: reported total= 3  ids=['reaction-r9','reaction-r8','reaction-r7']   TRUE disagreements=5
  reaction-r4: reported total= 3  ids=['reaction-r9','reaction-r0','reaction-r8']   TRUE disagreements=5
  reaction-r5: reported total= 3  ids=['reaction-r0','reaction-r1','reaction-r9']   TRUE disagreements=5
  reaction-r6: reported total= 3  ids=['reaction-r0','reaction-r1','reaction-r2']   TRUE disagreements=5
  reaction-r0: reported total= 6  ids=[...]                                          TRUE disagreements=7
  ```

  Four of the ten notes report `total == len(ids)`, so their chunks render as if the three ids were
  the whole story; the other six under-report by one or two.
- **Fix**: count and cap separately. `_widest_disagreements` already walks in descending order of
  gap, so it can keep counting after it stops *collecting*: return `(taken, total_seen)` and thread
  the count into `Conflict`-free tally that `_suspected` aggregates per note, then have `_strongest`
  take `total` from that tally instead of `len(ranked)`. The cheap alternative — stop capping in
  `_suspected` and cap only in `_strongest` — restores the true total but reinstates the quadratic
  scan the cap was introduced to kill, so it is not the one to take.

---

## The one substring haystack lower-cases SMILES, so benzene and cyclohexane are the same query

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/kg/search.py:52-56` (`search_text`),
  `:59-80` (`query_terms`), `:83-92` (`term_coverage`)
- **Trigger**: any query whose terms are a SMILES string, against a corpus holding the
  case-variant structure. `term_coverage` does `search_text(note).lower()` and `query_terms` does
  `query.lower()`, and SMILES case *is* the aromaticity flag.
- **Consequence**: `c1ccccc1` (benzene) and `C1CCCCC1` (cyclohexane) collapse to one token, as do
  `n`/`N`, `s`/`S`, `o`/`O`, `c`/`C`. Both notes come back with full term coverage and therefore
  land in the same `complete` bucket in `GraphRetriever.retrieve`, ranked only by confidence — the
  wrong molecule is served as current evidence, indistinguishable from the right one. This is not
  the incidental over-match the docstring concedes (`ester` in `polyester`): `compound_smiles` was
  added to this haystack *on purpose* so a chemist can search by structure ("14 notes were findable
  by their own `compound_smiles` in one and not the other"), and that is the feature that is wrong.
  It hits every consumer of the one haystack: `agent.graph_tools.find_notes`, `GraphRetriever`, the
  embedded text, and the `to_tsvector('english', …)` in `PostgresNoteIndex` (which also folds case).
- **Evidence**: `/tmp/repro_smiles_case.py`:

  ```
  query 'c1ccccc1' -> terms ['c1ccccc1']
     compound-benzene         coverage=1/1  haystack='compound-benzene compound c1ccccc1 Benzene.'
     compound-cyclohexane     coverage=1/1  haystack='compound-cyclohexane compound C1CCCCC1 Cyclohexane.'
     compound-pyridine        coverage=0/1  haystack='compound-pyridine compound c1ccncc1 Pyridine.'

  GraphRetriever('c1ccccc1') -> ['compound-benzene', 'compound-cyclohexane']
  ```

  The retriever, end to end over real note files on disk, returns cyclohexane for a benzene query.
- **Fix**: keep the structure out of the case-folded haystack and match it case-sensitively.
  Concretely: have `term_coverage` test each term twice — case-insensitively against
  `id + type + tags + body`, and case-**sensitively** against `compound_smiles` — counting a term
  covered if either hits. That preserves the prose behaviour exactly while making a structural term
  mean the structure. (Canonicalising the query through `core.chem.standard_smiles` when it parses
  as a SMILES would be better still, but it changes the term model; the case split is the minimal
  correct change.)

---

## The PR-gate proposal counter reports the state that was *asked for*, not the state that was stored

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/kg/proposal.py:314-323` (`_write` → `_count`)
- **Trigger**: the agent re-proposes a note whose byte-identical version was already **rejected**
  (or merged) by a human — the case both `InMemoryProposalStore.upsert` and
  `proposal_store._UPSERT` deliberately treat as "leave the decision standing".
- **Consequence**: `_write` calls `_count(proposal.state)` unconditionally after the upsert, so
  `chemclaw_note_proposals_total{state="open"}` increments although no row is open. The docstring
  says "Count one proposal reaching `state`" — it did not reach it. An operator watching the gate
  through this series sees a review queue that never drains, and (in the other direction) a
  re-proposal loop against a rejected note inflates the open count without bound. The store already
  returns the row id and could return the row.
- **Evidence**: `/tmp/repro_proposal_metric.py` — submit, reject, re-submit identical bytes:

  ```
  row state after re-proposal : rejected
  metric increments emitted   : ['open']
  ```
- **Fix**: have `ProposalStore.upsert` return the stored row (or its state) rather than the id
  alone, and count that. The Postgres statement can add `state` to its `RETURNING`; the in-memory
  backend already holds it.

---

## What I checked and found sound

Recorded so the negative results are not re-derived:

- **`kg.graph` caches.** `cached_notes`/`build_graph`/`invalidate_cache` under the TTL: the
  interleavings I could construct (a writer invalidating between a reader's fingerprint scan and its
  store) leave the cache holding *newer* content under an older fingerprint, which the next
  comparison busts. Self-healing, bounded by `graph_cache_ttl_seconds`. `conflicts._INDEX_CACHE` is
  not cleared by `invalidate_cache` but re-validates against the fingerprint, so it heals too.
  `_INDEX_LOCK` is only ever taken outside `_CACHE_LOCK` — no lock-order cycle.
- **`kg.conflicts._widest_disagreements`** two-pointer walk: the early `break` cannot skip a wider
  gap (both candidates sit at the ends of a confidence-sorted list), `other is note` uses `continue`
  after the pointer has already moved so it cannot spin, and `other_confidence` is carried out of
  the branch that chose it rather than re-derived — the bug its comment describes is genuinely
  fixed. `_overlaps` is a correct inclusive interval intersection.
- **`memory.ids.is_cluster_anchored`** round-trips for all three builders (`campaign-`,
  `playbook-`, `optimization-`) including the hyphenated `optimization-campaign` note *type*
  (`rpartition("-")` reads the id prefix, not the type), and correctly declines the
  scope-anchored promoted-observation playbook — so `supersede` does not retire it.
- **`memory.progression.progression`**: `zip([None, *ordered], ordered, strict=False)` pairs each
  run with its immediate predecessor correctly. `order_chronologically` is total and deterministic.
  `optimization_campaign_note` indexes `_quality_columns` cells by the same enumerate index that
  drives `zip(series.steps, members, strict=True)`, so no row/column mispairing.
- **SQL semantics.** `note_proposals` `_SELECT_MANY` has `ORDER BY id DESC` under its `LIMIT` and a
  keyset (`id <`) cursor, not an offset; `_DECIDE`'s `AND state = 'open'` and `_MARK_MERGED`'s
  `AND state = 'open'` are real optimistic-concurrency predicates; `_UPSERT`'s two `CASE`s read the
  pre-statement row, so their order does not matter. `dependencies` is `NOT NULL DEFAULT '[]'`, so
  `_proposal`'s `tuple(NoteFile(**f) for f in row["dependencies"])` cannot see NULL.
  `PostgresNoteIndex._dense`/`_lexical` scope arrays: `set()` renders as `'{}'` and correctly
  matches nothing (a different statement from `NULL`), and both retrievers short-circuit before
  sending one.
- **Async.** An AST sweep of all three packages found no unawaited coroutine expression, no
  `create_task`, no fire-and-forget. Every `_conflict_index` call is awaited; `fanout._sweep`
  catches per branch and `sweep_sources` restores source order by index rather than completion
  order, as its docstring claims.
- **`hybrid.reciprocal_rank_fusion`**: 1-based ranks, a note's best position per list only,
  first-encountered representative, deterministic `(-score, note_id)` tie-break. Correct RRF.
- **`retrieval.vectors`**: `InMemoryVectorStore` and `QdrantVectorStore` agree on the zero-vector
  short-circuit, the empty-scope-is-not-unscoped rule, the `> 0` floor and the `[0,1]` clamp;
  `_point_id`'s UUIDv5 is deterministic so re-embedding replaces rather than duplicates.
