---
name: campaign-narrative-synthesis
description: >-
  Judgment for turning a detected reaction chain into a citable campaign narrative: every
  claim traces to a member reaction note, the causal linkage is the fingerprint evidence,
  and nothing is inferred beyond what the chain actually shows.
---

# Campaign narrative synthesis

Holds the *judgment* for the episodic memory layer. The capability is deterministic:
`memory.chains.detect_chains` finds where a product of one reaction is a reactant of another
(structural identity via the fingerprint index), and `campaign_note_from_chain` builds the
factual skeleton — the ordered steps and the product→reactant handoffs, each wikilinking its
reaction note. This skill decides how to narrate that skeleton into something a chemist can
read and trust.

## Every statement cites its evidence

- The campaign note already links each member reaction (`[[reaction-…]]`). Keep it that way:
  every sentence of narrative must be attributable to one of those linked reactions. If you
  cannot point to the reaction that supports a claim, do not make the claim.
- The **causal edge is the shared compound**, nothing more. "The product of step 1 was carried
  into step 2" is supported (it's the fingerprint match); "step 1 was *optimized to enable*
  step 2" is an inference the chain does not license — do not assert intent or causality beyond
  the structural handoff.

## Narrate the chain, don't embellish it

- Describe what changed across the chain (the transformation sequence, yield trend if the
  notes record it), not a story you find satisfying. Missing data (an unrecorded condition, an
  unknown motivation) stays missing — say "not recorded", never fill it in.
- A campaign is episodic and project-scoped: it is *what happened here*, not a general rule.
  Resist generalizing — that is the playbook layer's job, under its own stricter gate.

## The gate still applies

- The campaign note is agent-authored and enters through the PR-gate (D-005). Write it so a
  reviewer can verify each claim against the linked reactions in one pass; that is the whole
  point of the citations.
