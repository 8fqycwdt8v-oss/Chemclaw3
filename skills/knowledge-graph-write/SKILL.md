---
name: knowledge-graph-write
description: >-
  Judgment for turning a finding into a knowledge-graph note — choosing its type,
  id, and links — and submitting it through the PR-gate for human review.
tools:
  - propose_knowledge_note
  - record_confirmed_answer
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

- **type**: the smallest accurate kind — `compound`, `reaction`, `job-result`,
  `campaign`, `optimization-campaign`, `playbook`, `report`, `failure-mode`,
  `interaction`. For anything you are proposing be *run* rather than reporting as
  done — a set of conditions for an untried substrate, the next step in a series,
  a batch from `suggest_next_experiment` — the kind is type `experiment-proposal`:
  one type for one decision a reviewer makes, whatever produced it. It must cite
  the evidence it rests on. Eval cases are *not* graph notes — they live under
  `eval_case_dir`, outside the graph (D-014).
- **id**: stable, human-readable, unique (e.g. `reaction-suzuki-<substrate>`);
  the id is how other notes link to this one, so it should not change.
- **links**: connect the note to what it relates to with `[[wikilinks]]` in the
  body — precursors, products, the campaign it belongs to, the source experiment.
  Links are the graph's value; a note with no links is nearly invisible.
- Set `confidence` and `valid_from`/`valid_to` honestly; record the `source`.

## Submitting

Every `created_by: agent` note goes through **`propose_note`** → a feature branch
and a review PR (D-005: the agent proposes, a human decides). Never write agent knowledge
straight to the main graph. Human-authored notes are committed directly.
