---
name: solvent-selection
description: >-
  Judgment for choosing a process solvent — what a computed solvent comparison actually
  measures (a continuum polarity effect on one equilibrium), the much larger set of
  criteria it cannot see, and why the green-chemistry and safety constraints usually bind
  first.
tools:
  - compare_solvents
  - compute_reaction_energy
  - predict_solubility
  - predict_pka
  - screen_hazards
  - find_notes
  - gather_evidence
---

# Solvent selection

`compare_solvents` runs the same reaction in each solvent and ranks them by free energy.
That is one input to a solvent choice, and rarely the deciding one. This skill is mostly
about the other inputs, because a recommendation that optimizes the computable criterion
and ignores the binding ones is worse than no recommendation.

## What the calculation actually measures

An implicit continuum (ALPB): each solvent is a dielectric that stabilizes charge and
polarity. So it can see one thing genuinely well — **whether the products are more polar
or more charge-separated than the starting materials**, and therefore whether a polar
medium shifts the equilibrium toward them.

It cannot see anything else about a solvent. Specifically absent, and each of these
routinely decides a real solvent choice:

- **Specific hydrogen bonding and coordination.** A solvent that hydrogen-bonds to a
  substrate, or coordinates a metal centre, behaves nothing like its dielectric constant
  suggests. THF and dichloromethane are not interchangeable to a Grignard reagent.
- **Solubility.** Of the substrates, of the product, of the salts and the base. This is
  the most common actual constraint and is invisible here (`predict_solubility` is
  aqueous only).
- **Rate.** The comparison is thermodynamic; solvent effects on rate are frequently
  larger and can point the opposite way.
- **Everything downstream.** Crystallization behaviour, phase separation in the workup,
  distillation and recovery, residual-solvent limits in the final API.

## Read the spread before the ranking

The result carries `spread_kcal` and an uncertainty. **When the spread is inside the
uncertainty, the calculation has not distinguished the solvents** — the result says so in
its warnings, and repeating the ordering anyway is the most confidently wrong thing this
tool makes possible. The correct answer then is that the thermodynamics do not choose,
and the choice should be made on the criteria below.

Compare against the gas-phase entry too. Little movement from gas phase to a polar
solvent means this reaction is simply not solvent-sensitive in the way a continuum can
model, which is itself a useful finding.

## The criteria that usually bind first

- **Safety and regulatory.** `screen_hazards` on every candidate. ICH Q3C classes are
  effectively a filter: Class 1 solvents (benzene, carbon tetrachloride,
  1,2-dichloroethane) are out; Class 2 carries limits that constrain the whole process.
  A computed advantage never outweighs this.
- **Green chemistry.** The published solvent-selection guides (CHEM21, and the
  pharmaceutical companies' own) encode real process experience. Prefer the recommended
  set — water, alcohols, esters, some ethers — and treat dipolar aprotics (DMF, NMP, DMAc)
  as flagged: they are reprotoxic and regulatory pressure on them is increasing.
  Chlorinated solvents are problem solvents for waste and exposure reasons regardless of
  how well they perform.
- **Scale-up practicality.** Boiling point against the reaction temperature, ease of
  removal, water miscibility for the workup, cost, recoverability, and whether the plant
  already handles it.
- **Precedent.** `find_notes` for the transformation. What the group has actually run
  outranks all of the above and every calculation.

## How to answer a solvent question

1. Precedent first.
2. Filter the candidate list by safety and green-chemistry class, before computing
   anything — there is no point ranking a solvent that cannot be used.
3. Run the comparison across the survivors, and read the spread before the order.
4. Give a recommendation with the reason, and name the criteria the calculation could not
   see. Where solubility or rate plausibly decides, say the screening experiment is what
   answers it — a small solubility check is cheaper than any calculation here.

A ranking may come back as a job id rather than a result, since a screen re-runs every
species per solvent. Report the id and poll it; see `reaction-thermodynamics`.
