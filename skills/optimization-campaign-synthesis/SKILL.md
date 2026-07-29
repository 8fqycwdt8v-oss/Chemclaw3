---
name: optimization-campaign-synthesis
description: >-
  Judgment for turning an optimization campaign (repeated runs of one transformation) into a
  cited analysis: read every output not just yield, attribute a change to the conditions that
  actually differ between runs, and never claim a lever the data does not show.
---

# Optimization-campaign synthesis

Holds the judgment for reading an `optimization-campaign` note — a set of same-transformation
runs the memory layer grouped by structural similarity (`memory.optimization`). The comparative
table (conditions × outcomes per run) is deterministic; the *analysis* is here.

## Read every output, not just yield

- The campaign is output-neutral: yield is one column, but the procedure prose behind each run
  (in its `[[reaction-<id>]]` note) carries impurities, observations, and robustness rationale.
  A question about purity, an exotherm, or reproducibility is answered from that prose, not the
  yield number. Expand the member notes.

## Attribute a change to what actually differs

- A "lever" is a claim that changing condition C moved output O. It is only supported when two
  runs differ in C (and ideally little else) and O tracks it. State the two runs you are
  comparing and cite both.
- Correlation across a messy screen is not a lever. If several conditions changed at once, say
  the effect is confounded rather than crediting one factor.
- Absent a controlled comparison, describe *what was tried and observed*, and stop there —
  that is still a useful answer.

## Discipline

- Every claim cites the member run(s) it rests on. Keep evidenced results separate from any
  extrapolation to untried conditions.
- The synthesized narrative is proposed through the PR-gate for a process chemist to approve
  (D-005) — it is a reading of the data, not new fact, until merged.
