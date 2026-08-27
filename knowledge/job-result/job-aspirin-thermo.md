---
artifact_refs: []
calc_refs: []
compound_smiles: CC(=O)Oc1ccccc1C(=O)O
confidence: 0.7
created_by: human
id: job-aspirin-thermo
relations: []
source: seed-corpus
tags:
- computed
- thermochemistry
type: job-result
---

GFN2-xTB thermochemistry for [[computed-from:compound-acetylsalicylic-acid]] at 298.15 K, 1 atm.

- electronic energy: -47.913421 Hartree
- Gibbs correction: +71.84 kcal/mol
- lowest mode: 38.2 cm^-1 (a real minimum)

Computed from a converged optimisation; when the Hessian behind a run like this is stored, the
same geometry at another temperature is arithmetic rather than a recomputation.

Seed content: no calculation row backs these figures, which is why `calc_refs` and
`artifact_refs` are empty here — a real job-result cites the calculation keys its numbers came
from, and `make kg-validate` checks every cited key against the calculation store.
