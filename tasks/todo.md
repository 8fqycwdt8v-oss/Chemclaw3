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
