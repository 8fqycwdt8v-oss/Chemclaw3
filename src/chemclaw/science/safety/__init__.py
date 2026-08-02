"""Three cited tables, three questions, deliberately not merged into one answer.

- `screen.py` (D-080) — process safety: structural alerts from a committed SMARTS rule table
  (`rules.yaml`) plus pairwise incompatibilities. "Is this safe to run today?"
- `genotox.py` — regulatory toxicology: DNA-reactive structural alerts (`genotox_alerts.yaml`)
  plus the nitrosamine formation route. "Will this need a control strategy?" An alert is never an
  ICH M7 classification.
- `ich.py` — the numbers: transcribed ICH Q3C residual-solvent limits and ICH Q3D elemental-impurity
  PDEs (`ich_q3c.yaml`, `ich_q3d.yaml`), each with the guideline, revision and table it came from.

All three are advisory input to a human review and none is a clearance. They are separate because
the questions are separate, and answering one with another is how a table of energetic motifs gets
reported as a regulatory verdict — see `science/safety/genotox.py` for the full argument.
"""
