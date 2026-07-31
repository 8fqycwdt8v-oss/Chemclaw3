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
  `experiment-batch`, `experiment-proposal`). Eval cases are *not* graph notes —
  they live under `eval_case_dir`, outside the graph (D-014).

  **Four of those propose rather than record, and picking the wrong one is easy**,
  because they differ by *how the suggestion was arrived at* and not by what it
  looks like. All four must cite the evidence they rest on.

  | type | one or many | where it came from |
  | --- | --- | --- |
  | `experiment-proposal` | one next run | your reasoning over the series, arguing from the record (`experiment-progression`) |
  | `experiment-batch` | a set of runs | `suggest_next_experiment` — a surrogate model's ask, with the design space that produced it |
  | `protocol` | neither — a procedure | a route or method you are proposing, not a point in a campaign |
  | `bo-candidate` | one best point | **not yours to write**: minted from a finished durable campaign |

  The line between the first two is the one that matters. If you reasoned your way
  to the suggestion, it is an `experiment-proposal` and the reasoning is the note.
  If a model proposed the points, it is an `experiment-batch` and the decision space
  is the note — a reviewer cannot judge "1.2 mol% Pd" without knowing whether the
  space went to 5 or stopped at 1.2.

  Three further types exist and are **not** yours to hand-draft, because a dedicated
  tool mints each one with provenance you cannot reconstruct by hand: an
  `interaction` note (a chemist-confirmed answer) comes from `record_confirmed_answer`,
  a `bo-candidate` note from a finished durable campaign, and a `failure-mode` note
  from the memory layer's synthesis of negative results. If one of those is what you
  want, call the tool — writing the type yourself produces a note that looks the same
  and carries none of the evidence.
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
