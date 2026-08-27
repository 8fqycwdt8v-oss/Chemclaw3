# The knowledge system, end to end — August 2026

**Point-in-time review. Not maintained.** Accurate as of 2026-08-27 against `2ebcea92`. What it
asks for is (or belongs) in [`docs/planning/BACKLOG.md`](../planning/BACKLOG.md); this file is the
record and the measurement behind those rows.

## 0. What this is

A whole-system audit of the knowledge layers: the note corpus (`knowledge/`), the kg package
(`src/chemclaw/kg/`), the write path (PR-gate, proposals), retrieval (`src/chemclaw/retrieval/` +
the agent tools that read it), the memory/mining tier (`src/chemclaw/memory/`,
`durable/memory_jobs.py`, `durable/observation_jobs.py`), and the cross-linking schemes that join
notes to calculations, ELN records, documents and published results. The question asked was: where
is it wrong, where does it silently lose work, where will it not scale, and where is it harder to
use than it needs to be — for both halves of the loop, generation *and* ablation.

**Method.** Five parallel audit passes, one per subsystem, each instructed to trace failure paths
in code rather than read docstrings, and to reproduce what could be reproduced. Findings marked
CONFIRMED were either traced to a specific line with the failure path in hand or executed and
observed; the highest-severity ones were then independently re-verified against the tree by a
second read before this file was written. Where a number is stated, it was measured on this
checkout (Postgres and Temporal running via `make up`; the full knowledge-related test slice —
`pytest -k "kg or graph or note or retriev or memory or proposal or crosslink or conflict"` — is
**621 passed, 0 failed** with the infrastructure up).

Baseline measurements taken for this review:

- `build_graph` on the shipped 38-note corpus: cold ~6.5 ms, warm 3 µs (cache hit inside TTL).
- A synthesized 10,027-note corpus: cold 1.84 s, warm 0.21 ms — the cache works; the cold parse
  and the post-TTL re-stat are the scale terms.
- `make kg-validate` on the shipped corpus: green — which §1 shows is itself a finding.
- `tests/test_warehouse_retriever.py` with Postgres down: **10 failures, not skips** — the one
  pg-backed file that does not gate on `tests/pg.py::migrated_db_or_skip`, so an offline
  `make test` goes red in a way that reads as a retriever regression (§6.6).

## 1. The corpus contradicts its own vocabulary, and the validator cannot see it

**The headline defect of this review.** `kg/relations.py` defines each relation's direction in its
own comments: `product-of` = "this compound is produced by that reaction", `catalyzes` = "this
species accelerates that reaction", `part-of` = "this note belongs to that campaign". The corpus
writes **twelve edges backwards** against that vocabulary — measured over all 38 notes:

| edge in corpus | count | declared direction |
|---|---|---|
| `reaction --catalyzes--> compound` | 3 | compound → reaction |
| `reaction --reagent-in--> compound` | 2 | compound → reaction |
| `reaction --solvent-for--> compound` | 1 | compound → reaction |
| `reaction --product-of--> compound` | 2 | compound → reaction |
| `reaction --precursor-of--> compound` | 4 | compound → compound |
| `campaign --part-of--> reaction` | 4 | reaction → campaign |

`knowledge/reaction/rxn-suzuki-biaryl.md:23` writes `[[product-of:compound-4-methoxybiphenyl]]`
*from the reaction*, while `knowledge/compound/compound-4-methoxybiphenyl.md:17` writes
`[[product-of:rxn-suzuki-biaryl]]` *from the compound* — the correct direction — so the graph
holds `product-of` edges pointing both ways, and `related(graph, x, "product-of")` returns a mix
of "reactions that produced x" and "compounds x produced" with no way to tell them apart.
`compound-acetylsalicylic-acid.md:23` is inverted in *meaning*, not just type: "Made from
`[[precursor-of:compound-salicylic-acid]]`" asserts aspirin is a starting material for salicylic
acid. And `tests/test_seed_corpus.py:79-82` **pins the inversion as correct** — it asserts
`related(graph, "rxn-suzuki-biaryl", "precursor-of")` returns the two compounds, so fixing the
corpus breaks the test that was written to prove typed edges work "against real content".

Why the gate is green: `kg/validate.py::_registry_problems` checks only that a relation *name* is
in the vocabulary. Direction and type-compatibility (a `catalyzes` edge runs compound→reaction; a
`part-of` targets a campaign/report/collection) are validated nowhere. Every edge above passes
`make kg-validate` cleanly.

Three more validator blind spots in the same class, each CONFIRMED by construction:

- **A typo'd frontmatter key is silently dropped.** `Note` has `frozen=True` and pydantic's
  default `extra="ignore"` (`kg/note.py:341`); `tag:`, `valid-from:`, `conditons:` all parse
  clean, validate green, and the data is gone. This is the exact failure `KNOWN_NOTE_TYPES`
  exists to prevent for the *value* of `type` ("a typo minted a new type silently"), unhandled
  for field *names*. Contrast `publish/record.py:473`, which sets `extra="forbid"` explicitly.
- **A note's directory need not match its `type`.** `validate()` checks `path.stem == note.id`
  only. A `type: playbook` note filed under `knowledge/compound/` passes — and the next agent
  proposal for that id writes `knowledge/playbook/<id>.md` via `pr_gate._note_file`, a second
  file claiming one id, where `_parse_notes`' first-in-path-order rule keeps the *old* file and
  drops the freshly merged one.
- **`external_citations` reports a graph note as a missing ELN record.** `validate.py:115-120`
  never subtracts corpus-defined ids, so a legitimate note whose id starts `reaction-` (a
  `KNOWN_NOTE_TYPES` entry, and `agent/graph_tools.py:277` says a human note under that name
  "must still win") fails `make kg-validate` on a correct corpus. `dangling_links` has the
  inverse blind spot: it skips every `reaction-` target unconditionally. The seed corpus dodges
  both only because it names reactions `rxn-*` — while `propose_knowledge_note`'s docstring
  steers the agent toward `"reaction-suzuki-x"` as its id example.

Smaller corpus-data defects, all CONFIRMED against the files: `playbook-recrystallisation-purity`
asserts "every reaction note states the purification with the yield" and 1 of 6 does;
`campaign-biaryl-scope` says "fixed catalyst and base" over members using K₂CO₃ and NaOtBu;
`report-biaryl-development` recommends SPhos, a ligand no cited note records; neither
`failure-mode` note carries `conditions.outcome` (the field whose docstring says a failure "must
not read as an ordinary run"); `valid_from` appears on exactly one note corpus-wide, so no run has
a date for `follows` chains or `is_current(as_of)` to order on; `job-boronic-acid-pka` has empty
`calc_refs` while asserting a computed pKa; `knowledge/README.md` still advertises "the hazard
gate", deleted by `D-2026-08-15-safety-is-a-tool-not-a-gate`, and cites "eleven `KNOWN_NOTE_TYPES`"
against a constant holding ten. There are **no note templates**: `make template-validate` validates
*step* templates, and the only scaffold for authoring a note is copying an existing one — which is
the direct cause of five distinct field-usage patterns within the `reaction` type alone.

## 2. Silent zero is the system's recurring failure class

`D-2026-08-01-a-cap-that-starves-a-source` fixed one instance — a retrieval leg contributing zero
chunks invisibly — and built per-branch observability for the *operator*. The class survives in at
least seven other places, none of which is visible to the *model* or the operator's alerts:

- **`vector`/`lexical` enabled without the index ever being built.** `note_reindex_enabled`
  defaults to `False` and is an independent switch from registry membership; nothing relates the
  two. A deployment that sets `CHEMCLAW_DATA_SOURCES=graph,vector,lexical` and forgets the flag
  queries an empty `note_index` forever: `chunks: 0, failed: false` on every sweep, and it never
  appears in `sources_failed` (`core/config/retrieval.py:244`).
- **Four `return []` paths that are semantically "could not ask" but report as "found nothing":**
  an un-entitled/anonymous caller zeroes the share leg (`ingest/documents/retriever.py:151`);
  any `gather_evidence(..., note_type=...)` call drops the share leg entirely
  (`documents/retriever.py:156` — `if filters.get("type"): return []`); a mis-pointed
  `knowledge_path` zeroes the dense and lexical legs (`retrieval/retrievers.py:450,494`).
- **`EvidenceSweep` cannot say which sources were asked.** It carries `sources_failed` but no
  per-source contribution counts, so the model cannot distinguish "the share leg declined on
  entitlement", "isn't configured", and "found nothing" — three different answers rendered
  identically (`agent/research_tools.py:233` names this and defers it).
- **Both miners can produce nothing silently.** `outcome_class` became optional (D-2026-08-26);
  a source that maps no outcome (or no `project`) empties both the playbook and observation
  filters in one line each (`memory/playbook.py:71-74`, `observation_mining.py:62-65`). The
  operator-visible signal for "your ELN binding will never generate a single playbook" is
  `recorded 0 finding(s)` — indistinguishable from a healthy quiet corpus.
  `observation_mining.py` declares a logger and never calls it.
- **An empty corpus read retires the whole observations tier.** `read_corpus` returns
  `complete=True` for zero reactions with zero skips (`durable/memory_jobs.py:103`); after
  `observation_retire_after_days` of a misconfigured source returning `[]`, `retire_stale` erases
  every open observation, and the tier's health signal reads as "the miners produce noise" — the
  exact wrong diagnosis.
- **`find_notes` truncates silently.** It breaks at `graph_max_results` in *alphabetical id
  order* with a `log.warning` only — the exact defect `EvidenceSweep.truncated_by` was introduced
  to fix for `gather_evidence`, unfixed in the sibling tool the system prompt tells the model to
  call next (`agent/graph_tools.py:143-150`).
- **`recall_observations(limit=50)` silently clamps to `observation_max_results`** (10), with no
  truncation marker (`memory/observations.py:317`).

Related coherence gap: `find_notes` requires full term coverage with no widening while
`gather_evidence`'s graph leg widens to partial matches (`graph_tools.py:141` vs
`retrievers.py:220`), so a four-term query the sweep answers returns "no current note contains
every word" from the tool the model is told to chain after it.

## 3. The write path: the gate looks safer than it is, and costs more than it needs to

The PR-gate pipeline (agent tool / miner → `propose_note` → worktree → branch push → human merge →
webhook → reindex → sidecar rsync) was audited end to end. Confirmed defects, in consequence order:

- **`--force-with-lease` is defeated one line above the push.** `git_submitter.py:469-481`
  fetches `note/<id>` into the remote-tracking ref immediately before pushing with the lease, so
  the lease is always "whatever is on the remote right now" and can never fail. A reviewer's
  fixup commit on a proposal branch is silently discarded by the next re-proposal —
  and `tests/test_knowledge.py:291` pins the overwrite as desired behaviour.
- **The `flock` is one pod wide; the deployed topology is multi-writer.** The lock file lives in
  each pod's `emptyDir` clone (`deploy/helm/chemclaw/templates/_helpers.tpl:424`), and
  `service.replicas: 2` with autoscaling to 6 means N pods hold N independent locks against one
  origin. Combined with the defeated lease: concurrent same-id proposals are last-writer-wins
  with no error. (The BACKLOG singleton-worker row names the Postgres advisory lock as the ~60
  line fix; this review adds that the lease defeat makes it more than a scaling concern.)
- **Every git failure is classified non-retryable, against three docstrings and a setting.**
  `durable/publish.py:62` lists `GitSubmitError` in `_BAD_DATA_TYPES` while
  `note_publish_retry()`'s own docstring says "only a genuinely transient `GitSubmitError` (dead
  remote) is retried". A 30-second network blip drops a note from a synthesis batch on the first
  attempt (`fan_out` logs and omits), `note_write_max_attempts` is dead for git faults, and
  nothing ever replays a `failed` proposal row — `ProposalState.FAILED` has no reader anywhere.
- **The proposal record and the branch can disagree, and both surfaces are trusted.** Branch is
  per-note (`note/<id>`), record is per-version (`(note_id, content_hash)`): re-proposing a
  changed note leaves the v1 row `open`, rendering bytes that exist on no branch; the merge
  webhook's `UPDATE ... WHERE note_id = ANY(%s) AND state='open'` then marks *both* rows merged.
  `POST /proposals/{id}/decision` with `approved=true` records `merged` with no git action and no
  reconciliation job comparing `state='merged'` rows against files on the base branch. The
  no-diff path returns a branch reference that was never pushed while incrementing
  `chemclaw_notes_proposed_total`.
- **A dependency file unconditionally overwrites the base branch's copy.** `compound_dependencies`
  re-mints the compound note from SMILES on every proposal that links it; a chemist's post-merge
  edit (hazard prose, a tag) is silently reverted inside a PR titled "Add job-result note".
- **Cost: every submission materializes the whole corpus.** The worktree is created *with* a
  checkout to make the symlink-containment check meaningful (`git_submitter.py:243` says so), and
  7–9 serialized git subprocesses run under a global lock — the mechanism behind the measured
  202 ms/note and the ORD backfill's 1.81 s/record, 4,251 branches. The docstring's revisit
  trigger ("if the corpus grows large enough to show up in submission latency") has already
  fired in the BACKLOG's own measurement; the two are not cross-referenced.
- **Ease: nothing opens the PR, and the agent never learns the outcome.** No git-host API call
  exists; a human browses `note/*` refs and merges by hand, and the merge webhook's payload shape
  is one "no host emits it" (`api/routes/proposals.py:40-51`) — operator glue nobody has written.
  There is no read tool over `note_proposals`: a rejection's mandatory reason is stored where the
  agent cannot see it, so the same note can be re-proposed forever, getting a success reference
  for a submission the store correctly refuses to reopen. A rejected proposal's branch also
  survives on the remote indefinitely.

## 4. Generation and ablation: the loop is not closed, and one claim in it is now false

- **The observations tier's central safety claim is false since D-2026-08-25.**
  `memory/observations.py:151` says evidence ids are "merged note ids, so an observation always
  points at knowledge a human already signed off". `observation_mining.py:98` mints
  `reaction-<id>` ids — which, since transcriptions moved to ungated `reaction_records` rows,
  are 100% auto-ingested evidence. A promotion PR's "supported by N merged notes across M
  projects" is a false statement to the human at the gate; the DB CHECK only blocks
  `observation-%` self-reference. The ADR said "the miners are unchanged" — which is precisely
  the defect.
- **The distilled playbook is a to-do note.** `memory/jobs.py:104-109` proposes a body that ends
  "Distil the transferable rule and conditions from the cited evidence." — an instruction to the
  reviewer. `skills/playbook-distillation/SKILL.md` (the actual judgment) is loaded only in chat
  turns; no durable path invokes it. The BACKLOG's "memory records; it does not change what the
  next turn does" row diagnoses this as "no automatic skill generation"; the narrower, cheaper
  defect is that the *existing* distillation judgment is never reached by the job whose docstring
  says it is.
- **Ablation is three unlinked mechanisms, two of them manual.** `supersede_updates` (automatic,
  plain-text successor line — untraversable by design), `close_refuted_note` (manual, wikilink),
  `retire_stale` (schedule, Postgres status column). None can see the others: a `failure-mode`
  note refuting a playbook does not stop `mine_corpus` re-observing the same cluster; there is no
  single "why is this note not current" query. And three retirement paths have structural holes:
  a run producing zero notes cannot retire anything (`jobs.py:79` short-circuits before
  `supersede_updates`); the per-run note cap can split a retirement from its replacement across
  days (`_slice_for_this_run` sorts by id; the pair have different ids); a promoted-observation
  playbook is retirable by *nothing* (`is_cluster_anchored` is false for it by construction).
- **The only trigger for knowledge generation is an LLM tool call.** After D-2026-08-25 the four
  miners' sole caller is `agent/durable_tools.py::synthesize_memory`; no CLI, no Makefile target,
  no API route. And `_memory_job_id` embeds the date, so "mine again after this afternoon's
  ingest" silently rejoins the morning's run and reports its id as success — the exact usage the
  tool's docstring recommends.
- **Duplicate-promotion guarding is per-pass.** The `promoted` subset check in
  `observation_jobs.py:100-114` is a local list: a subset promoted last week is invisible to this
  week's superset promotion (two playbooks for one finding, neither superseding the other), and
  an activity retry resets it mid-pass. `promoted` is also terminal — a promotion PR closed
  unmerged makes the finding invisible forever.

## 5. Cross-linking: five citation schemes, not one

| cited thing | syntax | shape-validated | existence-validated | bidirectional |
|---|---|---|---|---|
| note | `[[rel:id]]` | yes | yes (`dangling_links`) | in-memory only |
| note + metadata | `relations:` frontmatter | yes | yes | no |
| calculation | `calc_refs:` | yes | **no** | yes (`crosslink`) |
| artifact | `artifact_refs:` | yes | **no** | folded into calc |
| ELN run | `[[reaction-<id>]]` | prefix only | only with a live DB | no |
| document | — | **not citable from a note at all** | — | — |

- **A note cannot cite a document.** Document ids (`doc_id@chunking_key#ordinal`) are rejected by
  the wikilink slug grammar, and `EXTERNAL_ID_PREFIXES` is `("reaction-",)` — so a knowledge note
  can never point at the share document it was distilled from, while a report's evidence can.
- **Document citations cannot ground.** `EvidenceChunk.source_note_id` carries four incompatible
  shapes (note id, `reaction-<id>`, `<retriever>:<doc>#<ordinal>`, `tool-output-<n>`), and the
  verifier's `cited_ids` partitions every wikilink on the first colon — measured:
  `split_link("docs:abc123#4")` → `("docs", "abc123#4")`, which never equals the stored id, so
  every document citation in an answer scores as ungrounded.
- **`calc_refs` existence is checked nowhere.** The ELN citation half got a DB-backed CLI check;
  the calculation half never did, though the machinery (`crosslink.calc_ref_index`) exists and
  `kg/note.py:482` concedes the gap. A typo'd ref merges silently and indexes a key nothing
  produced. Publish has the inverse gap: `ResultRecord` carries no note field, while
  `job_records.note_id` does — two half-schemes for one question.
- Already queued and confirmed still open: `reaction_fingerprints` keys on the bare reaction id
  while `reaction_records` was re-keyed per source, so two sources' colliding entry ids share a
  fingerprint row and a `reaction-<id>` note id.

## 6. Performance and reliability of the read path

1. **The conflict scan is quadratic exactly when the corpus is dated.** The early-`break` in
   `conflicts.py:187` is reached only through `_overlaps`-passing candidates; closed validity
   windows `continue` without consuming the walk's budget. Measured: one substrate, one note/day —
   500 notes 14 ms, 1,000 → 170 ms, 2,000 → 714 ms, 4,000 → 3.1 s, clean 4× per doubling, on the
   retrieval hot path (`conflict_index` per `retrieve`), returning zero conflicts for the work.
   Every perf test in `tests/test_conflicts.py` builds windowless notes, so `_overlaps` is always
   true and the regression is invisible. Dated notes are what §1 says the corpus should have.
2. **A concurrent reader is served the pre-change corpus without waiting.** `_LAST_SCAN` is
   stamped *before* the parse (`graph.py:290`), so a second thread inside the TTL fast path pairs
   a fresh timestamp with the old `_NOTES_CACHE` entry. Reproduced: reader B returns the
   pre-change note list while A is mid-parse — against the docstring's "a waiter finds the answer
   it queued for". The window scales with parse time, i.e. with corpus size.
3. **`_text_is_writable` misses `ProcessConditions`.** The surrogate walk handles `str` and
   `list[str]` and does not descend into nested models; a surrogate in
   `conditions.major_impurity` builds a Note that raises `UnicodeEncodeError` in the PR-gate's
   commit — the exact failure the validator's docstring says it exists to prevent, in the field
   added after the enumeration was written.
4. **`search_text` omits `conditions` and `source`** (`search.py:46`), so the structured fields
   D-2026-08-25 added "so the numbers reach the note as frontmatter" are invisible to the
   substring sweep, the dense embedding *and* the lexical tsvector — an `outcome: failure` note
   is not findable by the word "failure".
5. **Retrieval scale terms, all confirmed:** the whole eligible-note set ships as a SQL array /
   Qdrant filter on every dense and lexical query (defeating HNSW per the module's own EXPLAIN
   note); a reindex embeds the entire changed set in **one** provider request with no chunking
   (all-or-nothing under retry); `GraphRetriever` is the only unbounded retriever (every scored
   note materializes an `EvidenceChunk` before the budget cuts to 40); the two merge modes dedup
   at different granularities (`(note, content)` vs `note_id`), so switching `retrieval_mode`
   changes chunk counts, not just order; the fingerprint retriever's whole filter path is
   unreachable from `gather_evidence` (`_AnchoredRetriever` hardcodes `{}`) and dead in the
   report path (prose query → `FingerprintError` → `[]`).
6. **Suite hygiene:** `tests/test_warehouse_retriever.py` fails (not skips) with Postgres down —
   10 red tests that read as a regression, against the repo's own `migrated_db_or_skip`
   convention. Also confirmed: the miners run their O(n²) similarity + full corpus parse
   synchronously on the shared `background-jobs` event loop (the one activity family that never
   got the `to_thread` treatment its sibling `observation_jobs.py:58` documents), and the corpus
   is DRFP-fingerprinted from scratch up to 4× per full synthesis.

## 7. What to do about it — ranked

Ordered by consequence-per-line-changed; the first four are small.

1. **Fix the corpus edges and teach `kg-validate` direction/type-compatibility** (§1). One pass
   over 12 edges + the pinning test, plus a per-relation `(source types, target types)` table in
   `relations.py` that `validate()` enforces. Everything in §1 currently merges green.
2. **`extra="forbid"` on `Note`**, directory-matches-type in `validate()`, corpus-defined-id
   subtraction in `external_citations`, nested-model recursion in `_text_is_writable` (§1, §6.3).
   Four small patches, each closing a silent-loss path.
3. **Kill the silent-zero class structurally** (§2): put per-source asked/contributed counts into
   `EvidenceSweep` itself (the fan-out already computes them per branch); make the entitlement
   and type-filter declines *declared* (`sources_failed` or a `sources_skipped` field); derive
   `note_reindex_enabled` from index-backed sources being enabled, the same move
   D-2026-08-26 made for `CHEMCLAW_CONNECTORS_ENABLED`; give `find_notes` the `truncated_by`
   marker its sibling already has; log the miners' empty-filter cases with the field that
   emptied them.
4. **Report renderer: render the evidence a partially-failed section kept** (§6). The
   `continue` at `harness.py:293` discards exactly what `gather_section` was changed to preserve;
   the fix is rendering the incomplete-marker *and* the chunks.
5. **Make the gate's concurrency honest** (§3): drop the lease-defeating fetch (first submission
   handles absence via push failure + one retry), move the submit lock to the Postgres advisory
   lock the BACKLOG row already sizes at ~60 lines, and split `GitSubmitError` into
   transient/permanent so `note_publish_retry` does what its docstring says.
6. **Reconcile the two review surfaces** (§3): supersede open rows on re-propose (same
   `note_id`, new hash → close the old row), give the merge webhook a version predicate, and a
   small reconciliation pass comparing `state='merged'` against files on the base branch.
7. **Close the loop's false claim and its dead ends** (§4): rename/reframe observation evidence
   as record-backed (and say so in the promotion PR body), give promoted playbooks a retirement
   path, make the supersede successor a real `superseded-by` relation on the *new* note (the
   dangling-link objection dies once the pair is proposed atomically — which the note cap must
   then respect), and add a non-LLM trigger (`make synthesize` / CLI) plus a forced-run override
   to `_memory_job_id`.
8. **One citation grammar** (§5): decide the document-id escape (the `:` collision means the
   current wikilink grammar can never carry them), validate `calc_refs` existence where the ELN
   half already validates, and take the fingerprint re-keying row.
9. **The two measured hot-path fixes** (§6): hoist `_overlaps` into the group partition in
   `conflicts.py`, and stamp `_LAST_SCAN` after `_NOTES_CACHE`.
10. **Authoring ergonomics** (§1, §5): per-type note templates (the current "template" is copying
    an inconsistent neighbour), `conditions` on `propose_knowledge_note` (the tool cannot write
    the field the seed-corpus test enforces), and `search_text` gaining `conditions`/`source`.

Batch submission for backfills (one branch per batch) and the reviewer-capacity arithmetic are
already queued in BACKLOG §"The PR-gate costs 1.81 s per proposed note" and are endorsed here
unchanged; the O(corpus) worktree checkout is the third leg of that same row.

## 8. What was checked and found sound

Worth recording so the next pass does not re-audit it: the graph cache's concurrency design (one
parse for eight cold callers — modulo §6.2's stamp ordering), duplicate-id first-in-path-order
resolution agreeing between the served graph and the reindex diff, deletion-safe index staleness
(`_chunks_from_hits` drops hits whose note no longer loads), the fan-out's failure accounting for
*thrown* failures (Postgres down degrades correctly, counted and named), `expand_note`'s typed
bidirectional edges (the right shape for a model), the worktree isolation and symlink-containment
tests, the proposal store's version/decision semantics in both backends, and the bi-temporal
window validation within one carrier. The system's own planning registers already carry the
`read_corpus` full-rescan, the `observations_status_idx` mismatch, the solvate id collapse, the
`within=` array scaling, and the O(n²) clustering trigger — those were verified as accurately
stated and are not repeated as findings.
