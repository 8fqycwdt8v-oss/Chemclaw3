---
name: calculation-selection
description: >-
  Judgment for choosing which fast calculator to run for a given question
  (semiempirical energy vs. predicted property) and reading the result honestly.
tools:
  - compute_xtb_energy
  - predict_pka
  - predict_solubility
  - predict_developability_profile
  - predict_logd
  - estimate_reaction_energy
  - submit_conformer_ensemble_job
  - get_conformer_job_status
---

# Calculation selection

Holds the *judgment* about the fast calculators; the mechanics live in the tools
(e.g. `compute_xtb_energy`). Use this to decide **which** calculator answers the
question and how far to trust it.

## Which calculator

- **Electronic energy / relative stability / conformer energy** → `compute_xtb_energy`
  (GFN2-xTB semiempirical single point on one seeded conformer). Fast, deterministic,
  good for *relative* comparisons of related structures.
- **Conformationally flexible molecule, solution-phase behavior** → `submit_conformer_ensemble_job`
  (a whole Boltzmann-weighted GFN2-xTB conformer ensemble, not one rigid geometry). This is a
  durable job (tens of xTB single points), so it returns a job id — poll with
  `get_conformer_job_status`. Reach for this instead of `compute_xtb_energy` when a single seeded
  conformer is unlikely to be representative (a flexible chain, multiple accessible rotamers).
- **Aqueous solubility** → `predict_solubility` (fast property model; reports an
  uncertainty — surface it).
- **pKa of an acidic O-H/S-H site** → `predict_pka` (GFN2-xTB solvated
  deprotonation energy + calibration; ~1.6 pKa-unit uncertainty). Only O-H/S-H
  acids (carboxylic acids, phenols, alcohols, thiols); it errors on molecules with
  no such site, and N-H/C-H acids are out of scope for now.
- **pH-dependent lipophilicity (logD)** → `predict_logd` (built on `predict_pka`, same domain
  limit: neutral O-H/S-H acids only). Use for HPLC mobile-phase pH selection, extraction, or
  formulation questions where the pH-independent LogP alone is not the number that matters.
- **Developability triage (Ro5/Veber, MW, LogP, TPSA, H-bond counts)** →
  `predict_developability_profile`. Report the flags as heuristics to weigh, never a pass/fail
  verdict on their own.
- **Reaction exotherm / thermal-hazard screen** → `estimate_reaction_energy` (sums cached
  GFN2-xTB energies over a balanced reactant/product equation). Advisory, like the structural
  hazard screen (`screen_hazards`) — a flag for attention, never a safety certification.

## Reading results honestly

- xTB energies are only meaningful **relatively** (same method, comparable
  systems); never present a single absolute Hartree value as a physical answer on
  its own — compare against a reference or a related molecule.
- Every result is cached, so exploring many related structures is cheap — do the
  comparison rather than reasoning from one number.
- If a property predictor reports an uncertainty, state it; if the question needs
  higher accuracy than a fast method gives, say so rather than over-claiming (the
  heavier QM/DFT path is deferred and would be the escalation).
