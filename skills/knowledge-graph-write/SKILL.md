---
name: knowledge-graph-write
description: >-
  Judgment for turning a finding into a knowledge-graph note — choosing its type,
  id, and links — and submitting it through the PR-gate for human review.
---

# Knowledge-graph write

Holds the *judgment* for adding to the knowledge graph. The capability is the
PR-gate (`kg.pr_gate.propose_note`); this skill decides *what* note to write and
*how* to relate it.

## When to write a note

Write one when a result is worth remembering and citing later: a job result, a
confirmed relationship, a campaign narrative, a distilled playbook. Do not write a
note for a transient intermediate — those live in the calculation store, not the
graph.

## Shaping the note

- **type**: the smallest accurate kind (`compound`, `reaction`, `job-result`,
  `campaign`, `optimization-campaign`, `playbook`, `report`, `protocol`,
  `experiment-batch`). Use `protocol` for an agent-proposed set of
  conditions/procedure and `experiment-batch` for a proposed set of next runs from
  `suggest_next_experiment` — both are proposals, not observed results, and must
  cite the evidence they rest on. Eval cases are *not*
  graph notes — they live under `eval_case_dir`, outside the graph (D-014).
- **id**: stable, human-readable, unique (e.g. `reaction-suzuki-<substrate>`);
  the id is how other notes link to this one, so it should not change.
- **links**: connect the note to what it relates to with `[[wikilinks]]` in the
  body — precursors, products, the campaign it belongs to, the source experiment.
  Links are the graph's value; a note with no links is nearly invisible.
- Set `confidence` and `valid_from`/`valid_to` honestly; record the `source`.

## Submitting

Every `created_by: agent` note goes through **`propose_note`** → a feature branch
and a review PR (D-005: AI proposes, human signs off). Never write agent knowledge
straight to the main graph. Human-authored notes are committed directly.
