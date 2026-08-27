---
artifact_refs: []
calc_refs: []
conditions:
  temperature_c: 80.0
  time_h: 12.0
  yield_percent: 76.0
confidence: 0.85
created_by: human
id: rxn-suzuki-biaryl
relations: []
source: seed-corpus
tags:
- cross-coupling
- suzuki
type: reaction
---

Suzuki-Miyaura coupling of 4-bromoanisole with phenylboronic acid.

- aryl halide: [[compound-4-bromoanisole]]
- boronic acid: [[compound-phenylboronic-acid]]
- product: [[compound-4-methoxybiphenyl]]
- pre-catalyst: [[compound-pd-oac2]] (1.5 mol% Pd)
- base: K2CO3 (2.0 equiv)
- solvent: [[compound-thf]]/water 4:1
- conditions: 80 °C, 12 h, degassed
- isolated yield: 76%

An electron-rich aryl bromide is a deliberately unfavourable substrate: oxidative addition is the
slow step, so this reaction is sensitive to ligand and temperature in a way an activated aryl
bromide is not. See [[playbook-pd-cross-coupling-scope]].

One of the couplings in [[part-of:campaign-biaryl-scope]]; the typed participant edges
(precursor, product, catalyst, solvent) live on the compound notes, which is the direction the
relation vocabulary runs in.
