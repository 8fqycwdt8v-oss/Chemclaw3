---
artifact_refs: []
calc_refs: []
confidence: 0.4
created_by: human
id: batch-suzuki-base-screen
relations:
- rel: part-of
  to: opt-suzuki-conditions
source: seed-corpus
tags:
- bo
- suggestion
- doe
type: experiment-batch
---

Four runs proposed for the next round of the biaryl coupling, from a single ask against the runs
recorded in [[cites:opt-suzuki-conditions]].

| # | base | solvent | T (°C) | Pd (mol%) |
| --- | --- | --- | --- | --- |
| 1 | Cs2CO3 | 2-MeTHF/water 4:1 | 75 | 1.2 |
| 2 | K3PO4 | 2-MeTHF/water 4:1 | 75 | 1.2 |
| 3 | Cs2CO3 | 1,4-dioxane/water 4:1 | 75 | 1.2 |
| 4 | K2CO3 | 2-MeTHF/water 4:1 | 65 | 1.5 |

Run 4 is the incumbent condition held back deliberately: a batch seeded only with what the model
already favours cannot discover that the model was wrong.

Two of these extrapolate beyond anything measured. No run on file uses K3PO4, and the surrogate is
reading it from computed descriptors rather than data. Temperatures stay at or below 80 °C because
[[evidence-for:failure-aqueous-protodeboronation]] is what the hotter condition buys.

These are proposals a chemist runs, not results. Nothing here has been measured.
