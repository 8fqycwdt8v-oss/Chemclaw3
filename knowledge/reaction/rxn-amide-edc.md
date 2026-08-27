---
artifact_refs: []
calc_refs: []
conditions:
  time_h: 16.0
  yield_percent: 81.0
confidence: 0.85
created_by: human
id: rxn-amide-edc
relations: []
source: seed-corpus
tags:
- amide-coupling
type: reaction
valid_from: 2026-04-14
---

EDC/HOBt amide coupling of benzoic acid with benzylamine.

- base: [[compound-dipea]] (2.5 equiv)
- solvent: DMF, 0 °C to rt, 16 h
- isolated yield: 81%

HOBt is not optional at this scale: without it the O-acylisourea rearranges to the N-acylurea and
the yield falls into the fifties. See [[evidence-for:playbook-amide-coupling-additive]].

The baseline run of [[part-of:campaign-amide-additive]].
