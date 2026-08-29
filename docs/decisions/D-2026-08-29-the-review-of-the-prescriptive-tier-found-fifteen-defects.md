# D-2026-08-29-the-review-of-the-prescriptive-tier-found-fifteen-defects — what an adversarial pass over `chemclaw.protocols` cost

**Decision.** An adversarial review of the merged prescriptive tier
(`D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not`) found **fifteen** defects, every one
of them under a green 185-test suite. All fifteen are fixed. Three of the fixes change behaviour that
the merged ADR describes, so this ADR records what it now does instead; the rest correct code that
never did what its own docstring said.

**The finding that outranks the individual defects:** four of the five worst were a *check that
could not fail*, and each was hidden by a passing test written from the same misunderstanding as the
code. This repository's own lesson is "the tests I wrote alongside a change cannot find the defects
in it"; here it cost four blockers, and the pass that found them was an agent with no stake in the
design, told to prove its suspicions with a script rather than an argument.

## The three that supersede the merged ADR

### An approval is about a document, so a new revision un-approves it

`store.advanced()` held `approved` across a new revision, reasoning that a re-draft must not silently
un-approve. That is true of the word and false of the thing. Measured: a chemist approves revision 1
at 80 °C, an agent drafts revision 2 at 200 °C, the header still reads `approved`, and
`GET /protocols/{id}` serves the head. **That is the one path in this tier to somebody running
conditions nobody signed off.**

`approved` now returns to `draft` on any revision, `request`-kind included — correcting the ask a
protocol was approved against un-approves it just as surely. Which revision *was* approved stays
recoverable from the append-only history. `abandoned` is deliberately still held: a design somebody
decided not to run does not come back because an agent wrote to it.

### The concurrency claim was the opposite of what happens

`PostgresDesignStore.append`'s docstring said two concurrent appends "cannot both see the same head".
`core.db`'s connections are READ COMMITTED, so they can, and do — measured on a real database with
no artificial barrier. The primary key on `(design_id, revision)` was the only thing stopping the
second, and it surfaced as a raw `psycopg.errors.UniqueViolation` that nothing translated: the
second chemist in "two chemists editing one plate is the ordinary case" got a **500 with no
`revision_conflict` code**, which is exactly the case the 409 was built for.

The violation is now caught and re-raised as the same `RevisionConflict` a stale `parent_revision`
raises. They are one fact — the revision you built on is not the head any more — reaching the writer
by two routes, and a caller that had to tell them apart would be a caller with two ways to do one
thing.

### A citation counts only when it is followable

`evidence_present` is the blocker the merged ADR calls load-bearing, and it read only `ref.kind`.
Measured: two `EvidenceRef`s carrying nothing but a sentence — no `ref`, no `tool` — cleared it. The
central claim ("use the record and the tools is a property of the code rather than a hope about the
prompt") was therefore false on the only turn it has to hold: the one where the model has an answer
it likes. A `tool` citation now needs a tool name and a grounding citation needs a `ref`, which is
how `hazard_screen_ran` was already written. Unfollowable citations are named in the detail rather
than silently uncounted.

## The four blockers that could not fail

- **`components_resolve`** tested `canonical == smiles and not _parses(smiles)`. `canonical_smiles`
  does not return the input unchanged on a bad input — RDKit stops at whitespace and at a non-ASCII
  edge, so `"CCO junk"` canonicalises *successfully* to `"CCO"`, a different and smaller molecule.
  The first clause is false for exactly that class, so the strict parser was never consulted on the
  inputs the check exists for, and the detail read `1 structures parse` about a structure that does
  not. It is `require_molecule`'s own recorded failure — `screen_hazards("CCO junk")` returning a
  clean screen *of ethanol* — one layer up. Now `not _parses(smiles)` alone; measurement confirms it
  cannot fail closed either, because the only inputs where the two parsers disagree the other way
  are over `molecule_max_atoms`, which both reject.
- **`forbidden_absent`**'s structure half could never fire for a named reagent.
  `canonical_smiles("DMF")` is the string `"DMF"`, compared against a set of canonical SMILES.
  Measured: forbidding "DMF" let a design charging `N,N-dimethylformamide` — same molecule — through
  a blocker. Both sides now go through `core.reagents.resolve_compound_name`, which never guesses;
  the written spelling remains the identity for reagents the table does not carry. The false-positive
  suspect was measured and cleared: forbidding `THF` does not match `2-MeTHF`.
- **`charge_is_consistent`** guarded its comparison with `reference.amount_mmol > 0` *inside* the
  comprehension, so a limiting reagent at exactly `0.0` — permitted, the field is `ge=0.0` — emptied
  the disagreement list and returned a passing blocker whose own detail read
  `limiting reagent 'SM' at 0 mmol`. Zero is the same fact as absent here.
- **`layout_fits`** validated neither `rows`/`columns` against `plate_format` nor a well's position
  against the plate. Reachable because the edit route accepts a whole `PlateLayout` from a browser —
  only `place()` was ever trusted. Measured: a layout declaring a 96-well plate as 1x2, with wells at
  row 98 labelled `ZZ99`, passed.

## The rest

- **`atom_balance` skipped the three-part reaction SMILES this tree itself emits.**
  `ingest.eln.ord.reaction_smiles()` produces `reactants>agents>products`, so an agent copying a
  precedent's reaction across brought a form the `">>" not in reaction` guard silently dropped —
  precisely when the precedent had a solvent or a catalyst. Split on `>` now; agents supply elements
  because they are in the flask. Its unreadable-species branch also returned a *passing* warning,
  which reads as "checked and fine" and which `render_markdown` (failed checks only) never showed.
- **`structure_experiment_request` replaced a drafted design's head with an empty ask.** The id is
  derived from the ask, so re-structuring reaches the same design; it then appended a bare
  `ExperimentDesign(request=…)` over the plate. `arm_count` reset to 0, the header stayed `draft`,
  and every default read served the empty ask. The procedure is now carried forward and the checks
  are graded at the stage the *design* is at.
- **`draft_experiment_protocol` deleted the plate on any revision that did not re-pass
  `plate_format`.** A revision changing only a temperature dropped the well assignments and the run
  order — and a randomised order is not recoverable, since a fresh `place()` with another seed is a
  different plate. The previous layout is carried forward; passing a format is what asks for a
  re-lay-out.
- **The edit route recorded a correction to the *ask* as a protocol revision**, graded at the
  protocol stage, reporting `is_a_protocol` and `evidence_present` as blockers. That is the failure
  `_REQUEST_STAGE` exists to prevent, reintroduced on the human path. `ExperimentDesign.has_protocol`
  is now the single definition all three callers read.
- **`diff.flatten` lost every duplicate key**, and `base.charge` is the dangerous one rather than
  `evidence`: a solvent charged in two portions is ordinary, and a chemist editing the *first*
  toluene line 5→9 mL was recorded as the *second* moving 2→9. Not a lost row but a misattributed
  edit, in the one table kept precisely to learn from those edits. A repeated key now falls back to
  `<label>#<index>` for every member sharing it, so an unambiguous list still diffs as reorder-immune.
- **Two model-level holes defeated two checks.** An unvalidated `replicate_of` exempted its arm from
  `arms_are_distinct` by naming an arm that does not exist; two factors sharing a name collapsed in
  `factor_levels_declared`'s `declared` dict, dropping the first factor's levels out of a *blocker*.
  Both are now `ExperimentDesign` validators, beside the arm-id uniqueness that was always there —
  a check answers about a design, and what makes a document well-formed at all is the model's job.
- **The two "real backends" disagreed about `session_id`.** `InMemoryDesignStore` overwrote it on
  every append while Postgres keeps the creator's, so `listing(session_id=…)` returned different
  designs depending on which was configured. Latent — nothing in `src/` passes that filter today —
  and fixed anyway, because a store whose answer depends on the deployment is a wrong answer on one
  of them.
- **Five docstrings and comments asserted what the code did not do**, three of them sitting directly
  on top of the defects above: the concurrency claim, `forbidden_absent`'s "matched on both the
  written name and the canonical structure", `components_resolve`'s "an unchanged string that RDKit
  would not re-read is the tell", a `plan_changed` contract that exists nowhere in `src/`, and a
  `protocols.request` module that was never written.

## What the review cleared, which is worth as much

`layout.py`'s arithmetic across all five plate formats, including `row_label` past row 25 (`AA`,
`AF`) — the defect was in `layout_fits`, not here. `components_resolve` false positives: none, the
strict/lenient mismatch only ever failed open. `forbidden_absent` false positives: none. The 2%
tolerance and its denominator, and the unreachability of negative amounts. `advanced()` on
`abandoned`. The 409 body's serialization. `get_protocol_diff`'s defaults, `list_protocols`'s clamp,
and the deliberate not-owner-scoping. And the whole grant/retention/erasure triangle — the
"enforced by grant, not by convention" claim holds.

## Cost accepted

Three behaviour changes against a merged ADR, each stated above. The `approved → draft` demotion is
the one a deployment would notice: a design under active revision no longer keeps a green badge, and
that is the point.
