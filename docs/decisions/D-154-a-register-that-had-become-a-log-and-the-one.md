# D-154 — A register that had become a log, and the one trigger it was hiding

**Context.** `docs/planning/DEFERRED.md` is one of the four files `CLAUDE.md` tells a session to read
at start. It had grown to 204 lines across **nine chronologically appended sections**, one per review
campaign. Nothing was ever removed from it: closure was recorded by *appending* a new dated section
explaining that an older one was now out of date. Asked to read it critically, the answer was not a
matter of style — five rows were false about the tree:

- **IDEA-2 (calibration), IDEA-1 (standing queries/digest) and IDEA-6 (corpus backfill)** were all
  implemented by **D-085** — the ADR immediately after the section that lists them as deferred, which
  was never revisited. The file called IDEA-2 "the most valuable remaining item" while
  `science/calc/calibration.py` and `tests/test_remaining_gaps.py` had pinned it for months, and
  called IDEA-6 a driver that "would be a stub" while `cli/backfill_corpus.py` existed.
- **"Wire the ORD adapter into the durable `ElnSyncWorkflow`"** was closed by D-054 plus D-120:
  `durable/eln_sync.py` iterates `active_ingest_source_names()` and `ingest/sources/eln-ord/` is a
  manifest folder, so enabling it is one config value and zero code.
- **The second queue system (pg-boss)** was never a deferral. Its trigger cell was a literal em dash,
  because D-006 *rejected* it.

Six further rows were struck through and annotated `**Done (D-0NN)**` — the file's way of saying
"closed" — and five topics were stated two to four times across different sections (spectra, the
budget quota, the substructure prefilter, the audit chain, the ingest contract), each restatement
subtly different from the others.

**Decision 1 — a closed deferral is deleted, not annotated.** The drift is structural, not careless.
Appending a status note below a stale row leaves the stale row readable as live state, and the next
campaign appends below *that*. `CLAUDE.md` now says the row is deleted in the commit that closes it:
the ADR is the record and `git log` is the history, which is exactly the argument D-147 made when it
removed `DECISIONS.md`'s shared append point.

**Decision 2 — group rows by what would have to change.** The nine campaign sections are replaced by
five: gated on infrastructure, on an upstream fix, on a scale not yet reached, on a capability or
licence, and deliberately declined. Grouping by *campaign* records when someone looked; grouping by
*blocker* is the only axis on which a trigger can be checked, and it makes the shape of the backlog
legible — the upstream group is four rows that all end in deleting a workaround, the scale group
states its measured current value (the graph holds 37 notes against a 10⁴ trigger). Every row names
an anchor in the tree so it can be verified with one `grep`. 204 lines became 93.

The declined table is kept rather than emptied, with an explicit "would reopen it" column, because a
decision that is merely absent gets re-proposed. TOOL-6 is the proof: it sat in this file as "blocked
on choosing a source", which read as an invitation, and was duly built against PubChem and removed
again (D-089).

**Decision 3 — implement the one trigger that was met.** The register's "compound notes so
molecule/substructure hits cite a note directly" row named a trigger — compound notes exist — that
D-083/D-084 satisfied, and the F11 section duly recorded the row as no longer deferred while nobody
did the work it described. Reaction hits have carried `reaction-<id>` since F11; molecule hits
carried a bare SMILES, and the tool docstring told the model to bridge with `find_notes` on each
SMILES — the literal substring path KM-4 flags, and precisely the bridge compound identity was built
to remove.

- **`compound_id` moves to `core.chem`**, beside the canonicalization it is built on. The id is a
  pure function of the structure; the kg import in `ingest.eln.compound` was for the note *builder*.
  That is what lets `mcp/molfp` cite a graph note without importing the graph (D-115), and it removes
  an existing reach-around in `connectors/qm/knowledge.py`. Not `core.ids`, as first planned: that
  module is deliberately domain-free hashing, and putting an rdkit-dependent function in it would
  drag rdkit into every importer of `stable_hash`.
- **The note id is derived at read time, not stored.** The alternative — re-keying the fingerprint
  index by `compound-<hash>` instead of by SMILES — needs a data migration to buy a lookup that is a
  hash of a column already present. `ReactionHit` has always derived its id the same way.
- **`MoleculeHit` replaces `SubstructureHit` and the molecule use of the shared `Match`**, and
  `agent/search_tools` re-exports it instead of keeping a third parallel model. The stored record id
  is dropped from the hit: for molecules it *is* the SMILES, so it carried nothing the `smiles` field
  does not. The one test that genuinely needed row ids — the `"C"` collation tie-break, where two
  records share one structure — moves down to `find_matches`, which is where the collation lives.
- **`compound_note_id` is `None` for a structure that no longer parses.** Ingestion canonicalizes
  leniently (`canonical_smiles` returns the input unchanged on a parse failure), so a junk label can
  reach the index; raising would let one bad row hide every real hit, which is already the rule the
  substructure scan follows when it skips an unparseable record.
- **The derivation is pinned to a literal** (`compound-f29e20f49d41` for ethanol), mutation-verified.
  A round-trip assertion would still pass if the scheme changed, while every merged
  `knowledge/compound/<id>.md` quietly became unreachable from a fresh hit.

**What the guard can and cannot do.** `tests/test_deferred_register.py` asserts no struck-through row,
no row without a trigger, and no citation to an ADR that does not exist — three shapes the file
actually had, each mutation-verified. It deliberately does **not** try to check whether a row's claim
is still true; no test can know that, which is why Decision 1 is the fix and the test is only its
backstop. A number merely `RESERVED` in the ledger counts as existing, for the same reason
`test_decision_log.py` exempts those rows.

**Found while verifying, and fixed here.** `agent/verifier.py` cited a `DEFERRED.md` row that has
never existed, and described D-032's durable hold as deferred when D-032 *shipped* it
(`durable/interaction_approval.py`). The docstring now says what is true, and the part that really is
unwired — routing a `review_required` answer into that hold — gets the row it was pointing at.

**Recorded, not fixed: two compound-id conventions.** The seeded corpus (D-135) names its nine
compound notes by slug (`compound-thf`), while the machine path mints `compound-<hash>`. The two sets
do not meet today — a hit exists only for a molecule in the fingerprint index, which is populated by
ingestion, which mints the hash-id note — so this is not a dangling citation. It is a duplicate-identity
hazard on the ingest path: ingesting 4-bromoanisole would produce a second note for a molecule the
seed corpus already describes. Renaming committed knowledge notes and the eight notes citing them is a
corpus-convention decision with its own review, not a rider on a docs cleanup, so it goes to
`BACKLOG.md` with the evidence rather than being resolved unilaterally here.

**Result.** `make lint type test` green. `DEFERRED.md` 204 → 93 lines, 36 rows, every one verified
against the tree; three stale rows deleted as shipped, three as decided-against, six struck rows
removed, five duplicated topics merged, three narrative sections dropped. New tests:
`tests/test_deferred_register.py` (4) and the citation half of `tests/test_compound_identity.py` (5).
