---
artifact_refs: []
calc_refs: []
conditions:
  outcome: failure
  temperature_c: 20.0
  yield_percent: 34.0
confidence: 0.75
created_by: human
id: failure-dcm-amide-coupling
relations: []
source: seed-corpus
tags:
- failure-mode
- amide-coupling
type: failure-mode
---

The EDC coupling in DCM at 20 °C gave 34%, not the ~70% the solvent screen suggested for a
chlorinated solvent.

Cause: the reaction was run at 20 °C throughout rather than starting at 0 °C. The O-acylisourea
rearranges at the higher initial temperature before the amine reaches it.

Recorded because "DCM is a bad solvent for this" is the wrong lesson —
[[cites:playbook-amide-coupling-additive]] holds, and the temperature ramp is what mattered.