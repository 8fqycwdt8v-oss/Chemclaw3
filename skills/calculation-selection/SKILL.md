---
name: calculation-selection
description: >-
  Judgment for choosing which fast calculator to run for a given question
  (semiempirical energy vs. predicted property) and reading the result honestly.
---

# Calculation selection

Holds the *judgment* about the fast calculators; the mechanics live in the tools
(e.g. `compute_xtb_energy`). Use this to decide **which** calculator answers the
question and how far to trust it.

## Which calculator

- **Electronic energy / relative stability / conformer energy** → `compute_xtb_energy`
  (GFN2-xTB semiempirical single point). Fast, deterministic, good for *relative*
  comparisons of related structures.
- **Aqueous solubility** → `predict_solubility` (fast property model; reports an
  uncertainty — surface it).
- **pKa of an acidic O-H/S-H site** → `predict_pka` (GFN2-xTB solvated
  deprotonation energy + calibration; ~1.6 pKa-unit uncertainty). Only O-H/S-H
  acids (carboxylic acids, phenols, alcohols, thiols); it errors on molecules with
  no such site, and N-H/C-H acids are out of scope for now.

## Reading results honestly

- xTB energies are only meaningful **relatively** (same method, comparable
  systems); never present a single absolute Hartree value as a physical answer on
  its own — compare against a reference or a related molecule.
- Every result is cached, so exploring many related structures is cheap — do the
  comparison rather than reasoning from one number.
- If a property predictor reports an uncertainty, state it; if the question needs
  higher accuracy than a fast method gives, say so rather than over-claiming (the
  heavier QM/DFT path is deferred and would be the escalation).
