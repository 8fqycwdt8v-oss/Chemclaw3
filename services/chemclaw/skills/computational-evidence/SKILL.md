---
name: computational-evidence
description: >-
  Judgment for deciding whether to compute at all — precedent first, calculation to rank,
  explain, or fill a gap — and for combining computed and retrieved evidence into one
  cited answer, including how a computed value is recorded and when to escalate to DFT.
tools:
  - find_notes
  - gather_evidence
  - compute_xtb_energy
  - compute_electronic_properties
  - predict_site_reactivity
  - submit_qm_job
  - propose_knowledge_note
---

# Computational evidence

Holds the judgment *above* the individual calculators. `calculation-selection` answers
"which calculator"; this skill answers the prior question — **should anything be
computed, and how does the number sit next to what we already know?**

## Precedent first, always

A calculation is never the first move. Before computing anything, check what is already
known (`find_notes`, `gather_evidence`): an ELN run on this exact compound, a
neighbouring analogue, a distilled playbook, a measured value.

Measured beats computed. A real result on a close analogue usually beats a computed
result on the exact molecule, because it carries the sterics, the reagent, the solvent
and the workup that no fast calculation contains. Say so when it applies rather than
computing to look thorough.

## The four honest reasons to compute

1. **To rank a set.** Twelve candidates, capacity for four. Semiempirical methods are
   good at ordering and poor at absolute values, so this plays to the strength.
2. **To explain an observation.** A result is already in hand and the question is *why*.
   Here calculation and precedent are checked against each other, and a disagreement is
   itself the finding — report it rather than picking the one you prefer.
3. **To fill a genuine gap.** Nothing on file, nothing analogous, and the answer changes
   what gets run.
4. **To triage before something expensive.** A fast screen ahead of a lab campaign, a BO
   campaign, or a DFT job.

If the request does not fit one of these, the calculation is decoration. Not computing —
and saying what evidence *would* settle the question — is a legitimate answer.

## Combining computed and retrieved evidence

- **Label every number.** A reader must never have to guess whether a value was measured
  or computed. Give the method with the value ("GFN2-xTB estimate", "measured, run
  ELN-2451"), not in a footnote.
- **Lead with the stronger evidence.** Usually the measurement. The calculation supports,
  contextualizes, or extends it.
- **State disagreements explicitly**, with a hypothesis for the discrepancy (sterics the
  descriptor cannot see, a solvent effect, the wrong tautomer, an unrepresentative
  conformer) and what would resolve it.
- **Never average them.** A measured and a computed value are different kinds of claim.

## What a computed value can carry, and what it cannot

Every result here is cached and content-addressed by structure, method and engine build,
so a number is reproducible and traceable to the exact geometry and stack that produced
it. That makes it *citable*. It does not make it *accurate* — reproducibility and
correctness are different properties, and the caveats in `calculation-selection`,
`reactivity-descriptors` and `ionization-and-partitioning` still apply in full.

Two standing limits of the fast tier, worth repeating because they are silent:

- **One conformer, at a force-field geometry.** Fine for ranking related structures;
  not a description of a flexible molecule's real behaviour.
- **Relative, not absolute.** Compare within a series against a stated reference. A lone
  absolute energy is not a physical answer.

## Recording a computed result

A computed value that matters beyond the conversation goes into the knowledge graph the
same way everything agent-generated does — drafted via `propose_knowledge_note` and
merged by a human through the PR-gate. Include the method, the uncertainty, and what the
value was used to decide, so the next reader can judge whether it still applies. Do not
record routine exploratory calculations; the calculation cache already keeps them, and a
graph full of unremarkable numbers makes the remarkable ones harder to find.

## Escalating

The fast tier is the bottom of a ladder. Escalate to the heavier QM path
(`submit_qm_job`, with `qm-job-submission` for the judgment) when the decision turns on a
difference smaller than the fast method's error bar, or when the question needs something
semiempirical methods do not provide. Say which it is.

Escalating is not automatic: a DFT job is slow and expensive, and "the fast answer is
good enough for this decision" is the right conclusion far more often than it is reached.
State the error bar, state the decision margin, and let the comparison decide.
