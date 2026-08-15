---
name: product-prediction
description: >-
  Judgment for "what will I actually get?" — enumerating the credible products of a
  transformation, ranking regioisomers from computed site reactivity plus precedent, and
  presenting a major product with the minor ones and the confidence the evidence supports.
tools:
  - predict_site_reactivity
  - compute_electronic_properties
  - compute_xtb_energy
  - compute_reaction_energy
  - find_notes
  - gather_evidence
  - screen_hazards
  - render_structure
---

# Product prediction

The question a chemist actually asks. This skill turns "reactants plus conditions" into a
ranked set of credible products — most often a regiochemistry call — and, just as
importantly, says how much confidence the evidence supports.

## Work in this order

**1. Precedent first.** `find_notes` / `gather_evidence` for this exact transformation, this
substrate class, this reagent. A recorded outcome on a close analogue outranks any
calculation, because it contains the reagent, the solvent, the temperature and the workup
that no descriptor does. If precedent settles it, say so and stop.

**2. Enumerate the candidates explicitly.** Write out the products that are actually possible
before ranking anything — the regioisomers, the over-reaction product, the double-addition,
the isomerized or eliminated alternative. A ranking is only as good as the list it ranks, and
the most common failure is a confident answer among two candidates when there were four.

**3. Identify the reactive sites.** `predict_site_reactivity` with the mode that matches the
chemistry: `electrophilic` when an electrophile attacks the substrate (aromatic
nitration/halogenation/Friedel-Crafts), `nucleophilic` when a nucleophile attacks it (addition
to a carbonyl, SNAr, conjugate addition), `radical` for radical chemistry. Getting the mode
backwards inverts the answer, so state which species is attacking which before you call it.

**4. Sanity-check against structural reasoning.** Directing effects, sterics, the reagent's own
bias, chelation or coordination control. The descriptors know none of these. Where the
calculation and the classical prediction agree, confidence is high; where they disagree, that
is the finding — report both and give the discrepancy a hypothesis.

**5. Screen the products for hazards** (`screen_hazards`) before recommending anything, exactly
as you would for the reactants.

## Ranking the candidates

- **Within one substrate**, the Fukui ranking is the primary evidence for *which site*. Compare
  the atoms of the same kind — ring carbons with ring carbons — and ignore the heteroatom
  topping the list on its lone pair (see `reactivity-descriptors`).
- **Between constitutional isomers of the product**, a relative energy can indicate the
  thermodynamic preference — but read `relative-energy-comparisons` first, and only if the
  reaction is plausibly under thermodynamic control. Most selective reactions are not.
- **Whether the transformation is favourable at all** is a separate question from which
  product forms, and `compute_reaction_energy` answers it for a balanced equation
  (`reaction-thermodynamics`). Worth asking when the proposed chemistry is unusual: a
  strongly uphill reaction that precedent does not support is a reason to re-examine the
  proposal rather than to rank its regioisomers.
- **Kinetic vs. thermodynamic control is a question you must ask explicitly.** Site reactivity
  is a kinetic argument; relative product stability is a thermodynamic one. They frequently
  disagree, and answering the wrong one confidently is the classic failure here. If the
  conditions do not tell you which regime applies, say so.

## What this cannot do

- **No barriers, no rates, no ratios.** You can rank sites; you cannot say "85:15". Never
  produce a numeric product distribution from these tools. A computed ΔG places an
  equilibrium and still says nothing about which product forms fastest.
- **Sterics are invisible.** A site can be electronically preferred and physically blocked.
  Where bulk plausibly decides, say the electronics do not settle it.
- **One conformer, one geometry.** For a substrate whose conformation controls which face or
  site is exposed, the ranking is indicative at best.
- **No catalyst or reagent model.** The calculation describes the substrate alone. A
  directed C–H activation, a chelation-controlled addition, or a reagent with its own strong
  preference can override substrate electronics entirely.
- **No mechanism.** The tools do not know whether this is SN1, SN2, radical, or
  concerted, and the answer often turns on that.

## Presenting it

Lead with the major product and the reason (electronic preference, precedent, or both).
Name the credible minor products — a chemist plans a purification around those, so omitting
them is worse than being uncertain about them.

**Draw them.** This skill's whole output is several closely related structures that differ in
one position, and that is exactly the case prose describes worst — "the 5-substituted isomer"
and "the 7-substituted isomer" are one numbering convention away from meaning the opposite
thing, while a picture is unambiguous and a chemist reads it faster. Call `render_structure`
on the major product, on each minor product you name, and on the transformation as a whole
when a reaction SMILES (`reactants>>products`) says it more cleanly than two separate
drawings. Keep the SMILES in the text as well: the drawing is for the reader and the string
is what the next tool call takes.

State the confidence honestly: "the electronics
and the classical directing effect agree, so this is a solid call" is different from "the ring
positions are electronically comparable, so the reagent and sterics will decide". Where the
answer matters and the evidence is thin, say what would settle it — a small test reaction is
usually cheaper than any calculation you could escalate to.
