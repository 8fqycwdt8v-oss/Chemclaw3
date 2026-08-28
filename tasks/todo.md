# The molecule / reaction / calculation cross-reference

**The question.** Can this system hold molecules extracted from reaction SMILES — starting
materials, products, reagents — each canonicalized and uniquely identified, cross-referenced so
that "which reactions did molecule X occur in" and "list every reagent" are answerable; and can
every in-silico result be stored against them with its full metadata (level of theory, solvent,
the workflow that produced it), including multi-compound systems such as a solvation free energy?

**The answer, measured against `HEAD` rather than the documents.** Most of it already exists, and
the parts that do not are smaller and more specific than they look. What follows separates the
three.

## 1 — What already works, and needs nothing

* **Molecule ↔ reaction ↔ role is a real relational table.** `reaction_species`
  (`infra/sql/051_reaction_labels.sql:78`) is one row per species per reaction, keyed
  `(source, reaction_id, ordinal)`, carrying `role` (the recorded vocabulary, `ingest/eln/ord.py:28`)
  and `derived_role` (the refined one, `science/labels/vocabulary.py:40`:
  `starting-material|product|reagent|solvent|catalyst|ligand|base|additive|unknown`), plus
  `scaffold` and `functional_groups TEXT[]`. Both of the questions above have a purpose-built index:
  `reaction_species (smiles, derived_role)` and `reaction_species (derived_role, smiles)`.
* **Canonicalization is one function on both sides.** `core.chem.standard_smiles`, so
  `reaction_species.smiles` and `corpus_molecules.id` join by value with no surrogate key
  (`054_corpus_molecules.sql:14`).
* **Local vectorization exists and is indexed.** ECFP4/2048 + RDKit pattern bits in
  `corpus_molecules` (HNSW + GIN); DRFP/2048 in `reaction_fingerprints` (HNSW). Both computed
  in-process by RDKit — no external service, nothing to reach.
* **The calculation model is fully designed** in `schema/result-store/001_core.sql`:
  `compound`, `structure`, `subject` + `subject_member` (a multi-compound system with per-member
  roles and stoichiometry), `theory_level` (method/family/basis/engine/treatment), `condition_set`
  (solvent, solvent model, T, p, pH, charge, multiplicity), `calculation` + `calculation_input`
  (the dependency DAG — "the workflow I got there"), `calculation_publication` (tenant, session,
  job, actor, rationale) and `property_value` with canonical units and uncertainty. The solvation
  example is the worked case, and `publish/project.py:930` builds the interaction subject with two
  `monomer` members and one `complex`.
* **Binding a citation is not a gap**, which an earlier reading of this got wrong.
  `FieldBinding.path` takes any dotted path, so a feed carrying only an id can bind
  `citation: {path: root.REACTION_ID}`, and `expr.TRANSFORMS` includes `default` for the rest.

## 2 — What is missing, in priority order

- [x] **A reaction feed cannot be vectorized or resumed** — the blocking one; see the spec below.
- [ ] **The result store has no live target** — [deployment, not code]. `result_sinks` defaults to
      `""` (`core/config/publish.py:35`) and `publish/sinks/postgres/sink.yaml` addresses a host
      nobody runs. Until a deployment sets `CHEMCLAW_RESULT_SINKS`, every structured calculation
      table above is DDL nobody has applied, and the only calculation store is
      `calculation_results` — `key → opaque JSONB`, whose query model refuses any predicate on the
      payload (`science/calc/store.py:256`). Consequence, stated plainly: **today, method, solvent
      and level of theory are not queryable**, because they live inside `input_hash` (a digest) and
      inside the payload. Already a `BACKLOG.md` row; the action is `make sink-schema`, apply,
      set the variable.
- [ ] **A published calculation does not name the reaction or note it belongs to** — [M].
      `grep -n "reaction_id\|note_id\|citation" src/chemclaw/publish/` returns nothing.
      `subject.kind='reaction'` exists and carries no reaction id, so a result published for the
      product of ELN reaction `EXP-1001` cannot be joined back to `reaction_records` or
      `reaction_species`. This is the literal "everything referenced to each other" gap.
      **Needs an ADR before code**, and the reason it is not taken in this pass is that the shape
      of the link depends on a live sink: `D-2026-08-26-a-route-is-not-a-shape` records the
      composite half of that path being inert for a whole release with no test noticing, because
      every test started at a projector rather than at a hook. Deciding a cross-reference against a
      store nobody has run is how that repeats. **Trigger:** the row above closes.
- [ ] **Structure identity is canonical SMILES and nothing else** — [M]. No InChI, InChIKey,
      formula, molecular weight, CAS or any external registry number exists anywhere in
      `infra/sql/` or `schema/`; `051_reaction_labels.sql:72` states the omission as a decision
      ("nothing asks, and this tree deletes dead columns"). Something now asks — an identifier that
      survives a `STANDARDIZATION_VERSION` bump, and one an external system can be joined on.
      **Needs an ADR before code.** The honest form of the decision is not "add a column" but
      "name the reader": an InChIKey with no query behind it is exactly the dead column that
      comment refuses. Candidate readers worth naming in that ADR: the result-store `compound`
      row (a site's other systems join on it), and a lookup path that survives re-standardization.
- [ ] **A solvate collapses onto its larger fragment** — [M], already a `BACKLOG.md` row under §2.
      Measured: `compound_id("CCN.C1CCOC1") == compound_id("C1CCOC1")` — ethylamine in THF and THF
      itself become one identity. `core/chem.py:230` keeps the largest fragment and
      `_identity_survives_stripping` guards only organometallics and reactive metals. **Fix this
      before loading a corpus keyed on `standard_smiles`**, because every row written under the
      collapsed identity has to be re-derived afterwards. The candidate fix and its measurement are
      already in the backlog row; this pass does not change it, but the ordering matters and is
      recorded here.

## 3 — The spec this pass implements: a reaction feed

**The requirement.** A daily job pulls `(reaction_id, reaction_smiles)` from an external ELN
database. The reaction data stays there; the **vectorization — of the reaction and of every
individual molecule in it — must happen locally**, in this database.

**Where it lands today.** That shape is a `corpus:` binding, not an `ingest:` one: the reaction
arrives already assembled as `reactants>agents>products`, and `CorpusBinding` is the block for
exactly that (`ingest/eln/warehouse/binding.py:644`). `ReactionCorpusWorkflow` already has a
**daily** Temporal Schedule (`corpus_sync_schedule_minutes`, default 1440), and `drain_corpus`
already splits the species into `reaction_species` with roles and writes ECFP + pattern bits into
`corpus_molecules`. **Molecule vectorization therefore already works, locally, today.**

Two things are genuinely missing, and both are additive.

### 3.1 The reaction itself is never vectorized on this path

`record_for_reaction` — the DRFP write — has exactly one caller, `ingest/eln/ingest.py:83`, on
the ELN path. `drain_corpus` writes no reaction fingerprint at all, so a feed ingested this way is
searchable by *molecule* similarity and not by *reaction* similarity.

**Decision: a second table, `corpus_reactions`, not more rows in `reaction_fingerprints`.** The
same argument `054_corpus_molecules.sql:8-13` makes for the molecule half, and it applies harder
here: `reaction_fingerprints.id` is the **bare** reaction id, which `ingest_reaction`'s own
docstring already records as unable to tell two sources apart — pouring millions of feed rows into
it would collide them with the ELN's own runs *and* swamp `similar_reactions` with hits whose
`reaction-<id>` citation resolves to a different record. `corpus_reactions.id` is
`<source>:<reaction_id>`, so it joins to `reaction_labels (source, reaction_id)` by construction.

Because the table carries the same five columns, `PostgresFingerprintStore` serves similarity over
it with **no new search code** — the same property `corpus_molecules` was built for.

**The bits are taken over `reactants>>products`, agents dropped.** `DrfpEncoder` folds the agent
slot onto the reactants (`rxnfp/fingerprint.py:29-30`), so passing the three-part form would let a
solvent swap dominate similarity — the effect `ord.py:333` measured at 0.82 for one coupling in THF
vs 2-MeTHF, 1.00 once excluded. The agents are not lost: they are rows in `reaction_species`, which
is the index built to answer *which solvent, which ligand, which base*.

### 3.2 A daily fire re-walks the whole corpus

`corpus_sync.py` and `schedules.py` both stated it before this pass: the keyset cursor is intra-run only and
there is no `sync_cursors` row, "because a re-drain of an unchanged release is a no-op and a *new*
release must be walked from the top". That is right for a versioned vendor release and wrong for an
append-only feed: every daily fire reads the entire corpus to discover the rows added since
yesterday. Correct, because every write is an id-keyed upsert — and O(whole corpus) per day.

**Decision: the binding declares the property, the drain does not guess it.** `CorpusBinding` gains
`append_only: bool = False`. It is a claim the binding author makes about the source — that
`order_by` is monotonically increasing for new rows — and it is stated as a claim because the cost
of it being wrong is a silently skipped row: a record back-dated below the watermark is never seen
again. A vendor release leaves it false and keeps today's behaviour exactly.

When it is true, the drain's cursor is persisted in `corpus_cursors (source, after, updated_at)` —
its own table rather than `sync_cursors`, whose `cursor` column is `TIMESTAMPTZ` and whose contract
is a datetime watermark. A keyset position is a `TEXT` key in the source's own domain, and storing
one in a timestamp column would be the shape mismatch this schema's own history keeps punishing.

### 3.3 What the other session has to do, and what it does not

**Does not:** write a cron runner, a fingerprint step, or any pull loop. The Temporal Schedule, the
paging, `continue_as_new`, the species split, the role assignment and both fingerprint writes are
all in the tree after this pass. Host cron is explicitly not the mechanism — durability lives in
Temporal (`durable/schedules.py:4`).

**Does:** add one `src/chemclaw/ingest/sources/<name>/datasource.yaml` and name it in
`CHEMCLAW_DATA_SOURCES`. Zero core edits (D-120). The binding it needs:

```yaml
binding:
  connection:        # the driver's own keyword arguments, checked against its signature offline
    ...
  corpus:
    relation: <schema>.<table>
    key: REACTION_ID          # the stable per-reaction id
    order_by: LOAD_SEQ        # monotonically increasing for new rows — see append_only
    append_only: true         # persist the keyset cursor; resume tomorrow where today stopped
    fetch_limit: 5000
    smiles:   {path: root.REACTION_SMILES}   # reactants>agents>products
    citation: {path: root.REACTION_ID}       # what a hit cites; any dotted path is legal
```

Then `uv run python -m chemclaw.cli.validate_datasources --construct` (the `make` target takes no
arguments — `make datasource-validate --construct` fails with `unrecognized option`),
`make schedules-apply`, and the daily drain exists.

**What the feed is then searchable by**, so the other session can check its own work: every species
in `reaction_species` with its role, every distinct structure in `corpus_molecules` (ECFP + pattern
bits, reachable through `conditions_for_similar_product` and `reactions_making_substructure`), and
every reaction in `corpus_reactions` (DRFP, reachable through `conditions_for_similar_reaction`).
If the last of those returns nothing on a drained feed, the drain wrote no fingerprints — check the
run's `unfingerprintable` count before suspecting the query.

**Two things to get right in that session, both of which fail quietly:**

1. `order_by` must be **unique and stable across the release**. A NULL in it holds the cursor where
   it is and stops that source with a warning (`corpus.py:130`) — the honest outcome, because a
   NULL makes the release un-resumable and nothing this side can invent changes that.
2. Query `reaction_species.smiles` with a **pre-canonicalized** SMILES. It is exact string equality
   on `standard_smiles` output, and there is no RDKit cartridge in Postgres — passing a raw user
   string silently returns zero rows rather than an error.

## 4 — Work items for this pass

- [x] `infra/sql/062_corpus_reactions.sql` — the table + its HNSW index.
- [x] `infra/sql/063_corpus_cursors.sql` — the persisted keyset watermark.
- [x] `src/chemclaw/science/labels/reactions.py` — `corpus_reactions()`, the id, and the
      transformation form. No class: unlike `CorpusMolecules` there is no extra column and no
      second search shape, so a constant and three functions is the whole module.
- [x] `CorpusBinding.append_only` in `ingest/eln/warehouse/binding.py`.
- [x] `drain_corpus` writes the reaction fingerprint; `corpus_sync` loads and stores the cursor.
- [x] `infra/sql/grants/app_privileges.sql`, `durable/retention.py`, `infra/sql/README.md`.
- [x] The **reader**: `Facet.reaction_keys`, `conditions_for_similar_reactions`, and the
      `conditions_for_similar_reaction` tool on the `rxnfp` bundle. Not in the first draft —
      see the review below.
- [x] `FingerprintStore.add_many`, so a page is one write rather than one per row.
- [x] Tests: the fingerprint write, the agent-drop, the resume, the release-mode no-op, the
      transformation precedent search on both backends, and the activity's own wiring.
- [x] The ADR, and its row in `docs/decisions/README.md`.

## 5 — Review

**What shipped.** Two migrations (`062_corpus_reactions`, `063_corpus_cursors`), one new module each
side of the seam (`science/labels/reactions.py`, `ingest/labels/cursor.py`), one binding field
(`CorpusBinding.append_only`), one new drain parameter and one new report counter
(`CorpusReport.unfingerprintable`), and the activity branch that reads and writes the watermark. The
workflow itself is unchanged: it already spelled "start of this source" as an empty `after`, which is
the only moment a stored position is worth consulting, so no `continue_as_new` payload moved.

**What was measured rather than argued.**

* Both migrations applied to the live database (`make db-migrate` → `applied 2 migration(s) in
  68ms`), and `\d corpus_reactions` shows the HNSW `bit_jaccard_ops` index built. The
  table-parameterised store then ranked over it with no new SQL — `tests/test_reaction_corpus.py`'s
  Postgres-backed case returns the seeded coupling at similarity 1.0 — which is the whole claim
  behind giving the table the same five columns as `003`.
* The agent drop is asserted on the *stored label*, not on the bits, because that is the string a
  reader sees and the one a future change would silently widen: `p1` is recorded with `CC#N` in the
  agent slot and indexed as `Brc1ccccc1.NC1CCCCC1>>c1ccc(NC2CCCCC2)cc1`.
* `append_only` defaults to `False` and is asserted to, since the change is additive *in behaviour*
  only as long as that holds.

**Four things this pass got wrong on the way and corrected, recorded so they are not re-derived:**

1. **`citation` was read as a blocker for a feed carrying only an id.** It is not.
   `FieldBinding.path` takes any dotted path, so `citation: {path: root.REACTION_ID}` is legal
   today, and `expr.TRANSFORMS` has `default` besides. Requiring a citation stays right.
2. **Writing corpus reactions into `reaction_fingerprints` looked like the smaller change.** It is
   the larger harm: that table is keyed on the bare id, so a feed would collide with this
   organisation's own ELN runs on any shared entry id, silently.
3. **`sync_cursors` looked like the obvious home for the watermark.** Its column is `TIMESTAMPTZ`;
   a keyset position is a key in the source's own domain.
4. **`_fingerprint_reaction` first caught `InvalidSmilesError` beside `FingerprintInputError`, and
   the docstring claimed an unparseable species was a cause.** Measured, it is not:
   `standard_smiles("C(((C")` returns `"C(((C"` unchanged, so DRFP shingles it and yields bits,
   while `ecfp_bitstring("C(((C")` raises — the two halves fail differently. The extra catch was a
   guard for a case that cannot occur; both it and the claim are gone, and a test pins the
   asymmetry so a later change to `standard_smiles` turns red instead of leaving a dead branch.

## 5b — What the review found, and what it changed

The first draft was reviewed against its own claims before merge, and **three of its findings were
defects rather than nits.** Recorded here because each is a shape worth recognising again.

1. **`corpus_reactions` was write-only.** The drain filled it; nothing in `src/` read it, because
   every reaction-similarity path binds `default_reaction_store()` → `reaction_fingerprints` and
   this ADR's own decision says corpus rows must not go there. So the defect the change opens with
   would have survived it, and what shipped would have been a store whose only evidence of working
   is that something writes to it. Fixed by building the reader the claim assumed:
   `Facet.reaction_keys` (the `source || ':' || reaction_id` narrowing that makes "joins by
   construction" true rather than notional), `conditions_for_similar_reactions`, and the
   `conditions_for_similar_reaction` tool. **The lesson: "the table is searchable" and "something
   searches it" are different claims, and only the second is a reader.**
2. **The per-row commit was justified by a benefit that does not exist.** "Resumable at the row
   rather than at the page" — but the cursor only advances when `drain_corpus` returns, so a
   retried activity re-reads the page from its start either way. Fixed by
   `FingerprintStore.add_many`, with `add` as its single-record case. **The review's own figure did
   not survive re-measurement and that is worth more than the fix**: it reported 1.07 s against
   0.09 s for 200 rows (~12x, ~19 h saved over 13M rows); re-run three times here it is 0.6 s
   against 0.23 s (**2.6x**), and batched 13M rows is still ~4 h of writes rather than ~11. The
   numbers in the ADR are the reproducible ones. A borrowed measurement is a claim about somebody
   else's afternoon.
3. **The workflow's "no cursor advance" guard was bypassed on an append-only source's first page.**
   It compared `page.cursor != state.after`, and those stopped being the same two values when the
   activity began resolving a *stored* position: `state.after` is `""` while the drain started at
   `A400`, so a stalled cursor read as an advance and the same page would be re-read every fire.
   Fixed by computing `advanced` in `drain_corpus`, the one place both values are in scope.

A second audit then caught two more, and both are the same failure mode as the measurement above:

4. **A tool with no probe.** `test_probe_coverage` is a gate — every agent-callable tool must appear
   in some eval probe's `expects_tools` or in `EXEMPT` with a pointer — and the new tool appeared in
   neither, so `make test` was red on the first commit. Probe `rx-41` closes it.
5. **The `0.85` agent-penalty figure was the most favourable case, quoted as if typical.** Measured
   across six solvents the range is 0.72-0.85 (MeCN, which I happened to pick, is the *top* of it),
   and a realistic ligand + base + solvent recipe scores **0.61** — below any sensible threshold,
   against the identical reaction. Quoting the best case understated a warning, which is the wrong
   direction to be wrong in. The docstring and the skill now carry the range and the recipe number.

Smaller corrections from the same pass: a claim that the corpus and ELN writes produce
"byte-identical rows" (they cannot — the labels differ by design), a claim that the workflow reports
`read`/`recorded` *per pass* (it returns one aggregate for all sources at the end of the chain, so a
stalled feed has no first-party signal — now stated as an open gap rather than an answered one), a
stale "three counters" comment invalidated by this same diff, a `CorpusReactions` class named here
that was never written, and `make datasource-validate --construct`, which is not a valid invocation.
Also fixed while building the reader: `_in_scope` and `_scope_coverage` are one condition written
twice, and adding a narrowing to only the first reported `total=10` against the in-memory backend's
`3` — the duplication is now named in `_scope_coverage`'s docstring with that measurement.

**Prose corrected because the code moved under it**, rather than left to go stale:
`corpus_sync.py`'s module docstring, the `run_timeout` comment in `schedules.py`, the `sync_cursors`
claim in `BACKLOG.md` §3, and the two package READMEs. `make prose-validate` passes.

**What this pass deliberately did not do**, with the reason and the trigger, now queued in
`BACKLOG.md`: the calculation↔reaction cross-reference (blocked on the results store having a live
target — deciding a join against a store nobody has run is how `D-2026-08-26-a-route-is-not-a-shape`
happened) and the structure-identifier question (needs an ADR that names a *reader*, and is ordered
after the solvate-collapse fix in §2, since any identifier minted before it inherits the collapse).

**What the suite run is and is not evidence about.** `make lint` and `make type` are clean
(710 files, mypy `--strict`). The full suite: **5321 passed, 9 skipped, 2 errors in 14m08s.**

Postgres and Temporal were started (`sudo dockerd`, `make up`, `make db-migrate`) before the run, so
the Postgres-backed set **executed rather than skipping** — including the new `corpus_reactions`
similarity case and two of the three `corpus_cursors` cases, which are exactly the assertions a
skipped run would have turned green without checking. The 9 skips are 6 `helm is not installed` and
3 `truncated history` in `test_migrations_are_additive`; none is a Postgres skip.

**One further failure is environmental and that was checked rather than assumed.**
`tests/test_reizman.py::test_bo_campaign_finds_high_yield` hits its wall-clock cap on this machine.
The suite says so itself ("these are wall-clock caps, not assertion failures … re-run with
`PYTEST_TIMEOUT_SCALE=4`"), and it reproduces on `origin/main`'s own content at 188.09 s against
this branch's 188.72 s — the same timeout, so nothing here caused it.

**The 2 errors are pre-existing and environmental, and that was checked rather than assumed.**
`tests/test_prompt_caching.py`'s two live-credential tests error at fixture setup with
`anthropic.BadRequestError: 400 … Your credit balance is too low`. Reproduced on the **stashed
baseline** (`git stash -u` → `10 passed, 2 errors`), so they pre-date this change and are unrelated
to it. Worth one note for whoever meets them next: that fixture is documented as costing "these two
tests and leaving the other 4,500 alone", and it *errors* rather than skipping when the provider
answers 400 — which is a small defect in the fixture's own guard, out of scope here.
