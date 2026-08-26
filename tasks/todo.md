# Solvent capability review — 2026-08-26

Started from a question: can the GFN2/MCP stack predict values in solvent, and answer
"which solvent is X more stable in" / "tautomer ΔG across solvents"?

Answer: yes, through GFN2-xTB's ALPB implicit continuum. Eight `servers/calc` primitives and all
nine `calc` durable jobs take a solvent; `compute_xtb_energy`, `predict_pka` (water-calibrated),
`predict_solubility` (ESOL, aqueous) and `predict_logd` do not. Reviewing that surface turned up
three things worth changing.

## Done

- [x] **`compare_solvents` was two capabilities under one name.** `props` served an MCP tool
      (tabulated properties, microseconds); `calc` declares a durable job (ΔG per solvent, minutes).
      Measured with both enabled: 21 endpoint tools + 9 jobs = 30 declared, 29 distinct. The registry
      checked job-vs-job only; `connector_tool_names()` is a set union and `_narrow` keys by name, so
      the loser vanished with no error.
      - [x] `registry._declared_tool_names()` refuses a collision across the enabled set — job/job,
            job/tool and tool/tool. `job_tools()` calls it; `connector-validate` inherits it.
      - [x] `props.compare_solvents` → `compare_solvent_properties` (Chemclaw3-mcp, same branch).
      - [x] Verified both ways against the real manifests: renamed → 72 declared, 72 distinct, loads;
            old name restored → `ConnectorError` naming both claimants and both kinds.
      - [x] ADR `D-2026-08-26-a-tool-name-is-one-capability-or-it-is-neither`.
- [x] **`props.compare_solvents` took an unbounded list** — a CONFIRMED/high finding from the
      2026-08-16 audit, never fixed. 100 000 x "dcm" = a 700 KB request (accepted, 70% of the 1 MB
      cap) returning 81 601 345 B after 14.83 s, `/healthz` stuck 14.47 s behind it. Bounded to the
      corpus size, asserted at the transport because `@server.tool()` returns the undecorated
      function and a direct call skips validation.

- [x] **`rank_species` took one solvent**, so "tautomer ΔG in water vs toluene" was N jobs and a
      manual diff — and not a diff either, since `species_ranking` sorts by energy, so the same
      index is a different form whenever the ranking reorders.
      - [x] `rank_species_across_solvents` + `SpeciesSolventScreenJobSpec` +
            `compose.species_solvent_comparison`, `solvent_comparison`'s shape applied to a
            distribution: gas phase prepended, same fan-out bound, budget counted over
            `species x media`.
      - [x] The result carries the per-medium distributions whole plus their transpose keyed by
            SMILES, `dominance_changes` as the headline, and the swing checked against the method
            uncertainty.
      - [x] `tests/calc_server_fake.py` gained `solvent_shifts` — its energy was a function of atom
            count alone, so no test could have observed a reordering.
      - [x] ADR `D-2026-08-26-a-solvent-is-an-argument-not-a-job`.
- [x] **Found while building it: `SpeciesDistribution` had no publish projector**, so `rank_species`
      published nothing. Added `_species_distribution` and
      `records_from_species_solvent_screen` (aggregate + one distribution per medium), eleven
      property registrations, and the `_cases()`/`_DELIBERATELY_UNREAD` entries so both new shapes
      are under the field-coverage test.

## Next

- [ ] **Three sibling composites still publish nothing** — `RefinedEnsemble`, `EnsembleProperty`,
      `BondDissociationSurvey`. Measured, `docs/planning/BACKLOG.md` row written, with the test that
      would have caught all four proposed there.

## Not doing, and why

- **A per-compound ΔG_solvation job.** Nothing computes one today (grep: zero hits outside the ALPB
  refusal message) and it is expressible as gas-phase vs solvated single points — but for one solute
  the answer is a solubility argument `props.solvent_swap_candidates` already makes from measured
  Hansen data, and ALPB's absolute solvation energies are much weaker than its relative ones. Worth
  an ADR deciding it, not a build.
- **pKa in a non-aqueous solvent.** `pka_solvent` is fixed at `water` and folded into `calc_version`
  as one of seven calibration settings. A second solvent needs a calibration set that does not exist
  here; it is a data problem, not a code one.

## Review

The two shipped changes are one defect each, both measured before and after rather than argued.
The registry guard is the half that generalises: neither repository imports the other, both grow
tool surfaces independently, and nothing made a name mean one thing across the set a deployment
enables. `compute_xtb_energy`'s docstring still invites comparing "the same molecule in another
solvent" while taking no solvent argument — reachable via `compute_electronic_properties`, which
returns the total energy, so it is a prose defect rather than a capability gap. Folded into the
next change rather than shipped alone.

---

# Rotational energies and rotamer barriers — implemented — 2026-08-26

## Task
"Get rotational energies and the barrier energy between rotamers for individual compounds —
especially, how the user tells the agent **which bond to rotate**." Concept first, then build it.

`D-2026-08-26-a-torsion-is-named-not-indexed` is the record; this is the working log.

## Plan
- [x] **1 · Read what exists** — `scan_coordinate`, `compose.scan_profile`, `thermo`, and the two
      skills that already hold the judgment.
- [x] **2 · Measure the premise** rather than assert it (RDKit 2026.3.5, the pinned build).
- [x] **3 · Decide** — three pieces, each on the side of a boundary already drawn.
- [x] **4 · `Chemclaw3-mcp`** — `enumerate_torsions` on `servers/chem`, plus `render_structure`'s
      `highlight_atoms`, plus the automorphism check and the contract table.
- [x] **5 · Here** — `torsion_handle`, `Torsion`/`Rotamer`/`RotationBarrier`/`RotationProfile`,
      Eyring in `thermo.py`, `rotation_units` in `budget.py`, `RotationJobSpec`,
      `compose.rotation_profile`, the activity dispatch, the manifest job, the projector and its
      properties, `rotational-barrier.yaml`, both skills.
- [x] **6 · Tests** — 41 new, driven through the real composite against a fake with a real
      torsional potential.
- [x] **7 · Verify** — `make lint type test`, `connector-validate`, `template-validate`,
      `skill-validate`, `prose-validate`; both repos.

## What building it found

1. **A stale atom index is not an error.** `(4, 5)` is the amide C–N of `c1ccc(NC(C)=O)cc1` and an
   aromatic *ring* bond of `CC(=O)Nc1ccccc1`. `scan_profile` bounds-checks and nothing else.
2. **The rotatable-bond descriptor is not a torsion list.** 0 for toluene, p-xylene and
   *tert*-butylbenzene; 1 for acetanilide, and that one is not the amide.
3. **Symmetry classes match automorphism orbits** on 21 molecules — 0 false merges. Shipped as a
   test, not as a claim.
4. **`skills/atropisomer-assessment`'s half-life anchors were wrong by two orders of magnitude.**
   Its prose said "27 → about a day"; 27 kcal/mol is 80 days, and 30 is 35 years, not "a few".
   The error was largest exactly at the ICH class boundary the skill exists to decide.
5. **Every `calc` durable job was publishing nothing.** `CalcJobWorkflow` sends
   `payload_kind=type(result).__name__` and its result is the `XtbJobResult` *envelope*, so
   `projector_for("calc.compute_reaction_energy", "XtbJobResult")` was `None` — while
   `tests/test_publish_reaches_the_hooks.py` was green asserting a `payload_kind` production has
   never sent. Fixed at the projection boundary (`unwrap_envelope`), not by re-shaping what the
   chat sees.

## Review

The three pieces, and why each is where it is:

- **`enumerate_torsions` on `chem`** (so, `Chemclaw3-mcp`): a pure graph operation, the sixth in a
  family of five, under the house rule *enumerate, then compute — and never the reverse*. It mints
  a handle from the canonical symmetry classes plus the RDKit build, so a rewritten SMILES keeps the
  name and a toolchain bump breaks it loudly.
- **`profile_rotation` here**: its key would name the wells it settles on, so `D-2026-08-16` says
  it is not shippable as a tool; it loops, so `D-2026-08-25-the-loop-is-a-composite-not-a-template`
  says it is not a template. Every point it computes is a separately-keyed primitive.
- **Eyring beside RRHO**: arithmetic over a result, not a calculation — the same rule that kept the
  RRHO half here when the physics left.

What is deliberately not done: 2D surfaces, transition-state claims, ring torsions, enumeration
inside the compute job. And the two open ends, both needing the live lane rather than more code —
no barrier has been computed against real xTB, and the conformer-dependence warning threshold is
unset. Both are in the ADR.
