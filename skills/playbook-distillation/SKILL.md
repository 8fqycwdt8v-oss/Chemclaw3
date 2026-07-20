---
name: playbook-distillation
description: >-
  Judgment for distilling a transferable playbook from patterns that recur across >=2
  projects: cite every supporting experiment, claim only what actually generalizes, and —
  in retrieval — keep evidenced history visibly separate from transferred analogy.
---

# Playbook distillation

Holds the *judgment* for the semantic memory layer. The capability is deterministic:
`memory.playbook.find_playbook_candidates` groups structurally similar reactions (DRFP) that
recur across **≥2 projects**, and `playbook_note` refuses to build a playbook without evidence
references. This skill decides what transferable rule, if any, those recurrences actually
support — and how to present it later without overstating it.

## A playbook is a claim about generality — hold it to that bar

- Distil only what recurs **across projects**. A pattern seen many times in one project is
  episodic (a campaign), not a playbook; the candidate finder already enforces the two-project
  floor — respect its spirit, don't launder a single project's habit into a "rule".
- Every playbook **must cite its evidence** (`[[reaction-…]]`) — the builder enforces this
  (Belegverweise verpflichtend). State the rule, then the reactions that support it, so a
  process chemist can check it. An uncited claim is inadmissible.
- Claim the **transferable** part (the transformation, the conditions that carried across
  substrates), not the substrate-specific detail. If only one condition actually generalizes,
  the playbook is about that one condition — say so, narrowly.

## Approval is a process chemist's call

- Playbook notes carry more authority than episodic ones (they will steer future work), so the
  PR-gate reviewer for a playbook is a process chemist, not just any human. Write for that
  reader: conservative, evidenced, explicit about the boundary of the claim.

## Retrieval: keep evidenced and analogy visibly separate (5.6)

- When answering a question using both layers, **do not blend them**. Present campaign/reaction
  evidence (what was actually done, with citations) separately from playbook guidance (a
  generalization transferred by analogy). Label which is which.
- Evidenced history outranks analogy: if a playbook's transferred expectation conflicts with a
  cited experiment in the current project, the experiment wins and the conflict is surfaced,
  not smoothed over.
