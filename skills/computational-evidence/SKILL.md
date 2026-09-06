---
name: computational-evidence
description: >-
  Judgment for deciding whether to compute at all — precedent first, calculation to rank,
  explain, or fill a gap — and for combining computed and retrieved evidence into one
  cited answer. Also holds the two loops that make a computed number worth more later:
  reaching a finished calculation's stored by-products (a relaxed geometry, a Hessian, a
  spectrum) so the follow-up question is cheap rather than a rerun, and reporting a chemist's
  measured value back so this deployment's own calculator accuracy is a measurement instead of
  a claim. Load it before quoting a computed value, before recording one, and whenever a
  chemist states an experimental number for something the system also predicts.
tools:
  - find_notes
  - gather_evidence
  - compute_xtb_energy
  - compute_electronic_properties
  - predict_site_reactivity
  - record_knowledge_note
  - find_calculations
  - list_artifacts
  - fetch_artifact
  - report_measurement
  - calculator_trust
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
   campaign, or a conformer search.

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

## A finished calculation is more than its answer

A calculation's *answer* is a handful of numbers, and `find_calculations` returns it. The run
usually also left files behind — the relaxed coordinates, the second derivatives, the raw
vibrational spectrum — and those are what make the *next* question cheap: thermochemistry at
another temperature off an existing Hessian, a conformer search seeded from a geometry that is
already a real minimum, a spectrum quoted band by band instead of described from memory.

The habit worth having: before proposing a rerun, ask `list_artifacts` what the stored run
already produced.

- **Order.** `find_calculations` for the calculation key, `list_artifacts` for what it kept,
  `fetch_artifact` for the one you need. A knowledge note's `artifact_refs` is the same kind of
  reference and can be fetched directly.
- **An empty listing is a normal answer**, not a missing calculation — most runs keep no
  by-products worth storing. Say "the run kept nothing", never "the calculation is gone".
- **Check the size before reading.** A Hessian is megabytes and exists to seed another
  calculation, not to be read into an answer. `fetch_artifact` refuses binary content and
  truncates large text at a configured ceiling; when it reports the content as truncated, say
  the value came from part of the file rather than presenting it as the whole.
- **Quote, do not paraphrase.** The reason to fetch a geometry or a spectrum is to state the
  actual coordinates or band positions. If you are only going to describe it qualitatively,
  the numbers already in the calculation record are the better citation.
- **By-products are eviction-managed.** A reference from an older note can point at something
  since reclaimed. That is a stale pointer, not a retracted result — report it as such.

## Closing the loop when a chemist reports a measurement

`calculator_trust` answers "how far has this calculator actually been off here?", and it can
only answer it from measurements someone reported back. That ledger does not fill itself: every
entry arrives through `report_measurement`, so an unreported measurement is one the next
person's trust question cannot use.

So whenever a chemist states an experimental value for a property this system also predicts —
a measured aqueous solubility as log S, a measured pKa — record it with `report_measurement`
in the same turn. It is cheap and it is not a knowledge-graph write at all: the
calibration ledger is the calculators' own store.

Read the reply literally and repeat what it actually says:

- **Reconciled against existing predictions** — the measurement scored them, and a later
  `calculator_trust` will reflect it.
- **Recorded with nothing predicted yet** — kept anyway, and the next prediction of the same
  thing scores against it. Worth saying, because it is the reason reporting it was not wasted.
- **Not recorded** — the deployment has the calibration ledger switched off. Then say exactly
  that: the value was not kept, nothing will be scored against it, and an operator has to
  enable the ledger. Do not report it as recorded, and do not call again.

The discipline this protects: never present a calculator's accuracy as established when the
ledger behind it is empty. "There is not enough logged experimental data here to judge trust,
and reporting measurements is what would change that" is the honest answer, and a confident
error bar derived from an empty store is the failure.

## Recording a computed result

A computed value that matters beyond the conversation goes into the knowledge graph the
same way everything agent-generated does — drafted via `record_knowledge_note` and
recorded straight into the graph, where the next person reads it unchecked. Include the method,
the uncertainty, and what the
value was used to decide, so the next reader can judge whether it still applies. Do not
record routine exploratory calculations; the calculation cache already keeps them, and a
graph full of unremarkable numbers makes the remarkable ones harder to find.

**"Matters beyond the conversation" has a moment, and this is it.** A comparison that
*settles* something — a solvent screen, a species ranking, a bond survey, a relative-energy
ordering — whose margin clears the method's stated uncertainty is a conclusion, and the
conclusion is the part no other store holds. The calculation itself is already kept twice
over: the cache holds every primitive by key, and a finished job's record holds its whole
result. What neither holds is *what you decided from it*, and that is what a later reader
comes looking for. So when a ranking clears its error bar and you have acted on it, propose
the note before the turn ends — the numbers survive without you, the reading does not.

Where the margin does *not* clear the error bar, the ceiling section below applies instead:
that is a real finding and it belongs in the answer, but a note whose content is "this
calculation could not distinguish them" is the unremarkable-number case above, and a graph
of them is worse than none.

## The ceiling

**Every method here is semiempirical, and there is nothing above it.** There is no DFT tier and
no cluster to send a calculation to. So when the decision turns on a difference smaller than the
method's error bar, or on something semiempirical methods do not provide, the answer is to say
so — not to quote the number anyway and not to promise an escalation that does not exist.

What to do instead, in order: check whether a cheaper part of the ladder here still settles it (a
conformer search where one rigid geometry was the real limit, a thermochemistry composite where a
single point was), and if none does, say what evidence *would* settle it — usually an experiment —
and propose it. State the error bar, state the decision margin, and let the comparison decide.
