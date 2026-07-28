---
artifact_refs: []
calc_refs: []
confidence: 0.8
created_by: human
id: opt-amide-solvent
relations: []
source: seed-corpus
tags:
- optimization
- solvent
type: optimization-campaign
---

Solvent screen for the EDC coupling: DMF, MeCN, DCM, 2-MeTHF.

- transformation: [[rxn-amide-edc]]
- DMF and MeCN comparable (81%, 79%); DCM 12 points lower; 2-MeTHF 76%

2-MeTHF within 5 points of DMF matters more than the ranking does — it is the one entry on this
list a process chemist can take to scale.