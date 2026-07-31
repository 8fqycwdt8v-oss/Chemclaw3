---
artifact_refs: []
calc_refs: []
confidence: 0.5
created_by: human
id: protocol-edc-amide-dmf
relations:
- rel: supersedes
  to: failure-dcm-amide-coupling
source: seed-corpus
tags:
- amide
- procedure
type: protocol
---

A proposed procedure for the EDC amide coupling, drafted from the runs in
[[cites:rxn-amide-edc]] and the solvent finding in [[evidence-for:failure-dcm-amide-coupling]].

1. Dissolve the acid (1.0 equiv) in DMF to 0.2 M under nitrogen.
2. Add HOBt (1.1 equiv), then EDC·HCl (1.2 equiv), and stir 15 min at 0 °C.
3. Add the amine (1.05 equiv) and [[reagent-in:compound-dipea]] (2.5 equiv).
4. Warm to ambient over 1 h; hold 12 h.
5. Quench into water, extract, wash the organics with dilute citric acid then bicarbonate.

DMF rather than DCM is the whole point of the draft — the DCM runs stalled at the O-acylisourea and
the rearrangement to the N-acylurea consumed the activated acid.

Unverified: nobody has run this. The stoichiometry is carried over from a related substrate and the
12 h hold is an assumption, not a measurement.
