---
name: safety-screening
description: >-
  Judgment for the structural hazard screen: when to call screen_hazards, how to report a flag
  so a chemist can act on it, why an empty result is never "safe", and where the screen's
  competence ends and a human process-safety assessment begins.
---

# Safety screening

Holds the *judgment* for `screen_hazards`, which matches structures against a curated,
literature-cited SMARTS table (`safety/rules.yaml`) and checks a reaction's components for
dangerous combinations. The tool is deterministic and has no opinion; this skill decides when to
call it and, more importantly, how to talk about what it returns.

## The one rule that matters

**The screen flags; it never clears.** No flag means *no rule in the table matched* — it says
nothing about toxicity, exposure, thermal stability of the specific compound, scale, impurities,
or the process around the reaction. Never write "the reaction is safe", "no hazards", "safe to
run", or any phrasing a reader could take as clearance. Say what is true: *"No rule in the hazard
table matched. This is not a safety assessment."*

An over-trusted screen is more dangerous than no screen, because it turns an absence of knowledge
into apparent assurance — and a chemist who has been told "no hazards" three times stops reading
the fourth answer.

## When to call it

- **Always, before proposing chemistry**: any synthesis route, reagent choice, condition set, or
  procedure you are about to recommend or write into a note. Screen every species you name —
  reactants, reagents, solvents, products, and any intermediate you propose isolating.
- **Pass the whole reaction, not one molecule at a time**, when several species meet. Component-wise
  screening cannot see an oxidizer/reductant pairing: each component is unremarkable alone.
- **On retrieved precedent too**, when you are about to recommend repeating it. A procedure being
  in the ELN means someone ran it once, not that it is safe to scale or to repeat unchanged.
- Not needed when you are only reporting a historical result and recommending nothing.

## How to report a flag

1. **Lead with it.** A high-severity flag goes at the top of the answer, not in a closing caveat.
2. **Give the explanation and the citation**, not just the rule id. "Contains an organic azide
   (energetic, potentially shock-sensitive; Bräse 2005)" is actionable; "flag: organic-azide" is
   not.
3. **Name what the chemist should do next** in their terms — a process-safety review before
   scale-up, in-situ generation instead of isolation, an SDS check, an EHS conversation. The
   rule's `explanation` field carries the standard control; use it.
4. **Do not soften or dismiss a flag** because the compound is common or the literature routinely
   uses it. Report it and let the human weigh it. Equally, do not editorialize a flag into a
   refusal: the chemist decides whether the work proceeds, with their controls.
5. **A flag is not a reason to withhold the chemistry.** Answer the question *and* flag the
   hazard; an answer that omits the route because it is energetic helps no one.

## Writing it into a note

Any agent-proposed note describing a procedure must carry a `## Hazards` section when the screen
returns a flag at or above the configured gate severity — `kg-validate` enforces this, so a note
missing it fails the PR check rather than reaching a reviewer without its warnings. Write the
section as the flags plus their standard controls, and state plainly that it is a structural
screen, not an assessment.

## Where this ends

The screen covers *structural motifs with a documented hazard*. It does not cover, and you must
not imply it does: toxicity or carcinogenicity, occupational exposure limits, regulatory or
transport classification, thermal-stability data (DSC/ARC), incompatibilities beyond the pairs in
the table, quantities and scale, or engineering controls. Those need an SDS, the site's EHS
function, and — for anything energetic — a process-safety review. When a question turns on one of
them, say so and point there rather than guessing.

**Computation does not extend the screen either.** A semiempirical calculation can estimate a
decomposition energy or the stability of an energetic motif, and a user will eventually ask for
one. It may be used to *triage* — to decide which compounds get sent for calorimetry first — and
it may never appear in an answer as reassurance. A computed number is not a DSC, an ARC, or a
process-safety assessment, and "the calculation says it is fine" is exactly the false clearance
the rule above forbids. Report the calculation as what it is, keep the screen's language
unchanged, and still point at the process-safety review.
