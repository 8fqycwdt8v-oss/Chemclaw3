# Atom-addressable reactivity — implementation

Concept: `tasks/reactivity-labels-concept.md` · ADR: `docs/decisions/D-2026-08-26-an-atom-index-is-not-a-name.md`

## ADR
- [x] `D-2026-08-26-an-atom-index-is-not-a-name.md` + ledger row

## Tier 0 — structural site labels (Chemclaw3-mcp / servers/chem)
- [x] `engine/sites.py`: `Site` + `describe_atom_sites`, one entry per symmetry class
- [x] content-addressed `site_id` on the `torsion_handle` construction
- [x] `describe_sites` tool, declared `read_only`
- [x] 27 tests: symmetry classes, ring relationships, C-H folding, handle stability

## Tier 1 — free descriptor panel (Chemclaw3-mcp / servers/calc)
- [x] read the ion energies `compute_fukui` was discarding
- [x] global panel (IP/EA/mu/eta/S/omega) + local (dual, s±, omega_k) + free valence
- [x] `test_the_panel_costs_no_extra_single_point` pins the SCF count at three

## Tier 2 — xtb-binary descriptors (Chemclaw3-mcp / servers/calc)
- [x] `engine/xtb_atomic.py` + `compute_atomic_descriptors`
- [x] property-table and ESP-grid parsers, written against a captured 6.6.1 run
- [x] refuses by name when the binary is absent; the *key* still derives (CREST's convention)

## Composition (Chemclaw3)
- [x] mirrored reader models, `CALCULATION_EPOCH` -> "2" in both repos
- [x] `compute_atomic_descriptors` on the `calc` bundle; `describe_sites` on `chem`
- [x] publish projector + property vocabulary carry the panel
- [x] `skills/reactivity-descriptors` rewritten: start with `describe_sites`, scope the
      question, aggregate by class, report only differences that exceed the class spread
- [x] probe `an-34` for the new tool

## Gate
- [x] Chemclaw3 `make check`: **4799 passed, 3 skipped** — with Docker/Postgres up, so the
      ~157 Postgres-backed tests really ran
- [x] Chemclaw3-mcp `make check`: **1188 passed, 5 skipped** — the 5 are the binary-only Tier 2
      tests, which run and pass with `xtb` installed (verified separately, 18 passed)

## Review

**What the concept got right.** The diagnosis held: the failure was presentation, not physics.
Phenol's *para* carbon is still rank 6 of 13 in the raw ranking, and scoping plus class
aggregation is what makes it reportable.

**What building it changed.**

1. **Tier 2 was verifiable after all.** `apt` carries xtb 6.6.1. Installing it replaced guesswork
   with captured output, and caught two things prose would have got wrong: the polarisability
   table is on *stdout*, not in `xtbout.json`, and an `--esp` run aborts (SIGABRT) after writing
   the grid and before the JSON — so a surface calculation cannot also carry the atomic multipoles.
2. **It exposed a live defect unrelated to this work.** `xtb_engine` defaults to `"auto"`, so with
   a binary present `compute_xtb_energy`, `compute_electronic_properties` and
   `predict_site_reactivity` stamped `+xtb+xtb-6.6.1` onto results computed entirely by tblite —
   none of the three has a binary code path. Fixed by making the backend a property of the
   **task** (`_FIXED_BACKEND`), not of the caller.
3. **`GetDefaultValence` is the wrong RDKit call for a free valence.** A sulfone's sulfur came out
   at −2.94. `GetValenceList` is the right one, and an element with more than one normal valence
   now gets `None`.
4. **Rounding a derivation separately from its inputs** made `f_zero` disagree with its own
   definition in the fourth decimal.
5. **A ring fusion is not a substituent.** Naphthalene was being labelled "bearing the CH
   substituent"; fused rings and two-heteroatom rings now refuse the classical *ortho/meta/para*
   names rather than misapplying them.
6. **A key derives without a binary; only computing refuses.** Got this backwards first;
   `test_deriving_a_key_runs_no_scf` caught it.

**Left open, deliberately.**

- **The cross-molecule claim for local electrophilicity is unsettled.** omega is 3.24 eV for
  phenol, 3.52 for *N,N*-dimethylacrylamide, 3.74 for pyridine — plausible ordering on a
  demonstrably wrong absolute scale. It ships as a ranking quantity for the calibration ledger to
  settle, not as an established one.
- **The xtb binary Hessian path produces no dipole derivatives.** Pre-existing — proven by
  stashing this change and re-running with the binary installed — so a deployment that adds xtb
  loses IR dipole derivatives and fails two `test_engine.py` assertions. Not this change's to fix;
  it needs its own decision about whether the binary Hessian route is supported at all.
- **No `profile_reactivity` composite tool.** The join, the class aggregation and the noise-floor
  rule live in the skill rather than in a cross-connector tool: the pieces are all free and
  `read_only`, and a composite spanning two connectors has no precedent here. If a second caller
  appears, that is the trigger to extract it.
