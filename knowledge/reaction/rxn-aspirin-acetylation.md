---
artifact_refs: []
calc_refs: []
compound_smiles: CC(=O)Oc1ccccc1C(=O)O
conditions:
  temperature_c: 60.0
  time_h: 2.0
  yield_percent: 88.0
confidence: 0.9
created_by: human
id: rxn-aspirin-acetylation
relations: []
source: seed-corpus
tags:
- esterification
- worked-example
type: reaction
---

Acetylation of salicylic acid with acetic anhydride to give aspirin.

- substrate: [[precursor-of:compound-salicylic-acid]]
- reagent: [[reagent-in:compound-acetic-anhydride]]
- product: [[product-of:compound-acetylsalicylic-acid]]
- catalyst: [[catalyzes:compound-dipea]] (2 mol%, in place of the classical mineral acid)
- conditions: 60 °C, 2 h, neat anhydride as both reagent and medium
- isolated yield: 88%

Yield determined by mass after recrystallisation from ethanol/water; see
[[measured-by:playbook-recrystallisation-purity]].
