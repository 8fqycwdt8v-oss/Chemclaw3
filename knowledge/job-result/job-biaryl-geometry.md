---
artifact_refs: []
calc_refs: []
compound_smiles: COc1ccc(cc1)-c1ccccc1
confidence: 0.7
created_by: human
id: job-biaryl-geometry
relations: []
source: seed-corpus
tags:
- computed
- geometry
type: job-result
---

GFN2-xTB optimised geometry for [[computed-from:compound-4-methoxybiphenyl]].

- inter-ring dihedral: 37.4°
- relaxation from the MMFF starting geometry: 4.2 kcal/mol

The twist is the point: a planar biaryl would conjugate through, and this one does not.

Seed content: no calculation row backs these figures, so `calc_refs` is empty — a real
job-result cites the keys its numbers came from, and `make kg-validate` checks each one against
the calculation store.
