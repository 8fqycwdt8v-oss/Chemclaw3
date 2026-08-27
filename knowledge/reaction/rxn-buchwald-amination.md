---
artifact_refs: []
calc_refs: []
conditions:
  temperature_c: 100.0
  time_h: 8.0
  yield_percent: 71.0
confidence: 0.8
created_by: human
id: rxn-buchwald-amination
relations: []
source: seed-corpus
tags:
- cross-coupling
- amination
type: reaction
---

Buchwald-Hartwig amination of 4-bromoanisole with morpholine.

- aryl halide: [[compound-4-bromoanisole]]
- pre-catalyst: [[compound-pd-oac2]] with RuPhos (2:1 L:Pd)
- base: NaOtBu (1.4 equiv), toluene, 100 °C, 8 h
- isolated yield: 71%

Shares its oxidative-addition step with [[analogue-of:rxn-suzuki-biaryl]], which is why the same
substrate scope conclusions transfer between them.

One of the couplings in [[part-of:campaign-biaryl-scope]].
