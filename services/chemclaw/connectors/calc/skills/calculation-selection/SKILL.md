---
name: calculation-selection
description: >-
  Judgment for choosing which fast calculator to run for a given question
  (semiempirical energy vs. predicted property) and reading the result honestly.
tools:
  - compute_xtb_energy
  - compute_electronic_properties
  - predict_site_reactivity
  - predict_pka
  - predict_solubility
  - optimize_geometry
  - compute_thermochemistry
  - scan_coordinate
  - compute_reaction_energy
  - compare_solvents
  - sample_conformers
  - predict_developability_profile
  - predict_logd
---

# Calculation selection

Holds the *judgment* about the fast calculators; the mechanics live in the tools
(e.g. `compute_xtb_energy`). Use this to decide **which** calculator answers the
question and how far to trust it.

For the prior question — *should anything be computed at all*, and how a computed number
sits next to retrieved precedent — load `computational-evidence`. Precedent outranks
calculation; this skill assumes that decision is already made.

## Which calculator

- **Electronic energy / relative stability / conformer energy** → `compute_xtb_energy`
  (GFN2-xTB semiempirical single point on one seeded conformer). Fast, deterministic,
  good for *relative* comparisons of related structures.
- **Conformationally flexible molecule, solution-phase behavior** → `sample_conformers`
  (a CREST metadynamics search, Boltzmann-weighted with rotamer degeneracies, reporting the
  conformational entropy a single-conformer free energy is missing). Minutes on a drug-sized
  molecule, so it returns a job id above the inline budget — poll with `get_durable_job_status`. Reach
  for it instead of `compute_xtb_energy` when a single seeded conformer is unlikely to be
  representative (a flexible chain, multiple accessible rotamers), and read
  `conformational-analysis` first: the search is *sampled*, so a missing conformer is not
  evidence of absence.
- **Aqueous solubility** → `predict_solubility` (fast property model; reports an
  uncertainty — surface it).
- **pKa** → `predict_pka` (GFN2-xTB solvated protonation/deprotonation energy +
  calibration). Two domains with different accuracy, and the tool says which one it
  used: **O-H/S-H acids** (carboxylic acids, phenols, alcohols, thiols; ~1.6-unit
  uncertainty) and the **conjugate-acid pKa of aromatic or aryl nitrogen** (pyridines,
  azoles, anilines; ±1.0, and the more accurate half). It **refuses aliphatic amines**
  and errors on a molecule with neither site; N-H/C-H acids are out of scope. Load
  `ionization-and-partitioning` before using the value for anything — an acid site wins
  silently over a basic one, and individual predictions miss by up to two units.
- **Which atom reacts (regioselectivity)** → `predict_site_reactivity` (condensed
  Fukui indices; three fast single points). Load the `reactivity-descriptors` skill
  before interpreting the ranking — it ranks sites *within* one molecule only.
- **Frontier orbitals, dipole, partial charges, bond orders** →
  `compute_electronic_properties` (one single point). Best used to *compare* related
  molecules; also covered by `reactivity-descriptors`.
- **A real 3D structure** → `optimize_geometry` (relaxes to a genuine GFN2 minimum).
  Everything above runs on a force-field geometry; this is the tool that stops that
  being true.
- **Free energy, entropy, an IR spectrum, "is this a minimum?"** →
  `compute_thermochemistry` (optimization + Hessian). The only route to a ΔG here.
  `computed-spectra-comparison` holds the judgment on the spectrum.
- **Does this reaction go?** → `compute_reaction_energy` (every species treated
  identically; balance enforced). Load `reaction-thermodynamics` first — it answers
  equilibrium and says nothing about rate.
- **Which solvent?** → `compare_solvents`. Load `solvent-selection` first; the
  computable criterion is rarely the binding one.
- **Rotational barrier, torsion profile, ring strain** → `scan_coordinate`
  (`conformational-analysis`, and `atropisomer-assessment` for the regulatory case).

## The cost ladder, and when a tool hands back a job id

Roughly: a single point is milliseconds; an optimization is under a second for a small
molecule; a Hessian is 6N of those; a reaction is that per species; a solvent screen is
that per solvent. The expensive tools estimate their own cost and, above a threshold,
return a **job id instead of a result** — report it and poll with `get_durable_job_status`
rather than treating it as a failure. Prefer `level="quick"` when only an ordering of
electronic energies is needed; it skips every Hessian.
- **pH-dependent lipophilicity (logD)** → `predict_logd` (built on `predict_pka`, and it
  inherits that tool's domain exactly: O-H/S-H acids and aryl-nitrogen bases, with
  aliphatic amines refused). It applies the Henderson-Hasselbalch correction in the
  direction the site calls for, so say which site the number belongs to. Use for HPLC mobile-phase pH selection, extraction, or
  formulation questions where the pH-independent LogP alone is not the number that matters.
- **Developability triage (Ro5/Veber, MW, LogP, TPSA, H-bond counts)** →
  `predict_developability_profile`. Report the flags as heuristics to weigh, never a pass/fail
  verdict on their own.
- **Reaction exotherm / thermal-hazard screen** → `compute_reaction_energy`, whose result
  carries `is_strongly_exothermic` against a configured threshold. Use `level="quick"` when the
  flag is all you need: it skips every Hessian and differences electronic energies only.
  Advisory, like the structural hazard screen (`screen_hazards`) — a flag for attention, never
  a safety certification.

## Reading results honestly

- xTB energies are only meaningful **relatively** (same method, comparable
  systems); never present a single absolute Hartree value as a physical answer on
  its own — compare against a reference or a related molecule.
- Every result is cached, so exploring many related structures is cheap — do the
  comparison rather than reasoning from one number.
- If a property predictor reports an uncertainty, state it; if the question needs
  higher accuracy than a fast method gives, say so rather than over-claiming (the
  heavier QM/DFT path is deferred and would be the escalation).
- The **fast** calculators (single point, properties, Fukui, pKa) run on a force-field
  geometry, not a GFN2-optimized one. Fine for ranking and for comparing related
  structures; when the question is about a specific conformation or needs a real
  stationary point, `optimize_geometry` is the fix and costs almost nothing.
- Everything here still describes **one conformer**, optimized or not. That limit does
  not go away with a better geometry — `conformational-analysis` says when it
  invalidates an answer rather than merely widening it.
