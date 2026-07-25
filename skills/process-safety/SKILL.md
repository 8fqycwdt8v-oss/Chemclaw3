---
name: process-safety
description: >-
  Judgment for screening a proposed route, protocol, or set of conditions for
  process-safety hazards before proposing it, and for reporting what the screen
  does and does not cover.
---

# Process safety

Holds the *judgment* around `screen_hazards`; the screen itself is deterministic and lives in the
tool. Load this whenever the answer includes a **proposed** protocol, route, solvent/reagent
change, or set of conditions — not when merely reporting what an experiment already did.

## The rule

**Call `screen_hazards` before proposing chemistry, and fold the result into the proposal.**

Pass every species involved: substrates, reagents, catalysts, solvents, the quench, and any
intermediate you are proposing to form or isolate. Names and SMILES both work (the screen resolves
them the same way `resolve_compound` does), so pass what the evidence actually says.

A proposal that skips the screen is not finished. This is the one place where being wrong has
physical consequences rather than informational ones, and the PR-gate reviewer sees a
plausible-looking procedure — a reviewer is a *check*, not a *screen*.

## Reading the result honestly

- **`critical`** — do not propose the combination. Say what the hazard is, and propose the
  alternative (the finding's guidance usually names one: a different solvent, in-situ generation
  instead of isolation).
- **`high`** — propose it only with the control named in the guidance stated *in the protocol
  itself* (a temperature ceiling, a charge order, a quench sequence), never as a footnote.
- **`caution`** — carry it into the protocol as a handling note.

## What the screen does not do

It recognises energetic structural motifs, a curated set of substance hazards, and known
incompatible pairs. It is **advisory and incomplete by construction**.

- Anything listed in `unresolved` was **not screened at all**. Say so explicitly. Never let a
  report with no findings read as a clearance for species the screen could not identify.
- No findings means "nothing recognised", never "safe". Phrase it that way.
- Thermal stability of a *specific* mixture at a *specific* scale is a calorimetry question
  (DSC/ARC), not a structural one. Where scale-up is in scope, say that the data is needed rather
  than implying the screen substitutes for it.
- The screen knows nothing about the plant, the containment, or the operator. It screens
  chemistry, not an operation.

## Interaction with the knowledge graph

A proposal that reaches `propose_knowledge_note` becomes a candidate precedent other projects may
reuse, so the hazard annotation belongs **in the note body**, not only in the chat answer. A
knowledge note that records conditions without recording the hazard that constrains them is worse
than no note: it will be found later, out of the conversation that qualified it.
