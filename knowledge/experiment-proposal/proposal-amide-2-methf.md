---
artifact_refs: []
calc_refs: []
confidence: 0.5
created_by: agent
id: proposal-amide-2-methf
relations:
- confidence: 0.5
  rel: follows
  to: rxn-amide-edc
source: seed-corpus
tags:
- amide-coupling
- proposal
type: experiment-proposal
---

Proposed next run on the amide coupling, reasoned from the series rather than from a surrogate
model — the counterpart to [[cites:bo-amide-next]], which proposes a reagent change from a model
fitted on the same runs.

- **run**: EDC/HOBt, 2.5 equiv DIPEA, **2-MeTHF** in place of DMF, 0 °C to rt, 16 h. Everything
  else held at the conditions of [[follows:rxn-amide-edc]].
- **rationale**: DMF is the only solvent the series has used, so solvent is untouched rather than
  established; [[cites:failure-dcm-amide-coupling]] shows the coupling is solvent-sensitive in a
  way the yield alone did not reveal. 2-MeTHF is the cheapest way to test that sensitivity in the
  favourable direction.
- **expectation**: isolated yield within ~5% of the 81% in DMF. A materially lower yield refutes
  the reading that the DCM failure was about solvation of the O-acylisourea and points at water
  content instead.
- **fallback**: if 2-MeTHF underperforms, hold DMF and move the additive, not the solvent.
- **not answered**: nothing here separates solvent from workup — the isolation is unchanged, so a
  yield difference could still be recovery.

Seed content: the chemistry is illustrative, the shape is what matters — a proposal that cites the
run it answers, states what would refute it, and waits for a human to approve it (D-005).
