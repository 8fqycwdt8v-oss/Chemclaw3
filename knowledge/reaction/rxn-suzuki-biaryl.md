---
artifact_refs: []
calc_refs: []
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

- aryl halide: [[precursor-of:compound-4-bromoanisole]]
- boronic acid: [[precursor-of:compound-phenylboronic-acid]]
- product: [[product-of:compound-4-methoxybiphenyl]]
- pre-catalyst: [[catalyzes:compound-pd-oac2]] (1.5 mol% Pd)
- base: K2CO3 (2.0 equiv)
- solvent: [[solvent-for:compound-thf]]/water 4:1
- conditions: 80 °C, 12 h, degassed
- isolated yield: 76%

An electron-rich aryl bromide is a deliberately unfavourable substrate: oxidative addition is the
slow step, so this reaction is sensitive to ligand and temperature in a way an activated aryl
bromide is not. See [[playbook-pd-cross-coupling-scope]].