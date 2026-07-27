---
name: molecular-association
description: >-
  Judgment for questions about two molecules together — API with excipient, substrate with
  additive, host with guest — reading a computed interaction energy honestly, and knowing
  that an interaction energy is not a binding free energy.
tools:
  - compute_interaction_energy
  - compute_electronic_properties
  - predict_pka
  - find_notes
  - gather_evidence
---

# Molecular association

Every other calculation here describes one species. `compute_interaction_energy` describes
a **pair**, which opens a set of questions nothing else could reach: does this excipient
interact with the API, does an additive bind the catalyst, what holds this co-crystal
together, why does this compound refuse to leave that solvent.

## The one thing to get right

**An interaction energy is not a binding free energy.** Two molecules becoming one costs
translational and rotational entropy — roughly 6-10 kcal/mol at room temperature for a
small pair — and that term is *not in the number*. Consequences:

- A pair with an interaction energy of −3 kcal/mol is, in all likelihood, **not bound** in
  solution at room temperature. Do not report it as a complex.
- Only strong interactions — hydrogen-bonded networks, charge-assisted pairs, large
  dispersion surfaces — survive the entropy cost.
- **Comparisons are far safer than absolutes.** Two excipients ranked against the same API
  share the entropy penalty, so the *difference* is meaningful where neither absolute is.

Say "the interaction is worth about X kcal/mol, before the entropy cost of association",
never "they bind by X".

## What it is good at

Measured against high-level reference values, GFN2 handles these well: the water dimer
comes out at −4.97 against a reference −5.0, the ammonia dimer −2.9 against −3.1, the
methane dimer −0.4 against −0.5. So:

- **Dispersion-dominated contacts** (aromatic stacking, alkyl surfaces) are a strength —
  the method carries a modern dispersion correction and it shows.
- **Neutral hydrogen bonds** are good to a few tenths of a kcal/mol.
- **Ranking a series** against one partner is the most reliable use of all.

## Where to be careful

- **Charged pairs.** An ion pair's interaction energy is enormous in the gas phase and
  almost entirely cancelled by solvation. Always run these with a solvent, and treat the
  magnitude as indicative only.
- **The search is stochastic.** A binding mode that was not sampled is not reported. Few
  binding modes found usually means the search was too quick, not that the pair binds one
  way — raise `effort` before concluding anything about specificity.
- **One pair, no bulk.** Real association happens in a solvent full of competitors. A
  computed API-excipient interaction says nothing about what happens in a formulation
  where water is present in vast excess.
- **No stoichiometry beyond two**, and no crystal. A co-crystal is a lattice question;
  this can say whether two components have a plausible synthon, not whether they
  co-crystallize.

## How to use it well

1. **Precedent first** (`find_notes`). Excipient compatibility and co-former screening are
   heavily documented areas.
2. **Ask a comparative question.** "Which of these three excipients interacts most
   strongly with the API" is answerable; "does the API bind this excipient" is not.
3. **Run in the relevant solvent**, especially for anything polar or charged.
4. **Report the ranking, the magnitude, and the entropy caveat together.** Then say what
   would settle it — a compatibility study, a DSC, a solubility measurement.

**It is not a fast tool.** A complex search is a metadynamics run plus three optimizations,
so for drug-sized partners it is minutes, and above the inline budget the tool returns a
job id instead of a number. Say that a search is running rather than going quiet, and
prefer one well-posed comparative question over sweeping a list.
