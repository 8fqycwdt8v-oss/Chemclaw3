---
name: relative-energy-comparisons
description: >-
  Judgment for using GFN2-xTB total energies — what an energy *difference* between related
  structures supports (an ordering), what it does not (a magnitude, an absolute value, a
  flexible molecule), and when the comparison is not valid at all.
tools:
  - compute_xtb_energy
  - optimize_geometry
  - compute_reaction_energy
---

# Relative energy comparisons

`compute_xtb_energy` returns a total energy in Hartree. On its own that number means nothing
— it is not a heat of formation, not a stability, not anything a chemist can use. It becomes
useful only as a *difference* between two comparable structures, and this skill is the rules
for when that difference is trustworthy.

## The comparison must be valid before it is accurate

An energy difference is only meaningful when both species have the **same atoms** — the same
molecular formula and the same total charge. Comparing an alcohol with its ether isomer is
valid; comparing an alcohol with its acetate is not, because the difference then includes
whatever atoms the two do not share and is a meaningless number rather than an imprecise one.

Check this first. It is the failure that produces answers wrong by hundreds of kcal/mol while
looking entirely ordinary.

Valid comparisons: constitutional isomers · tautomers (same formula) · conformers ·
cis/trans and E/Z pairs · regioisomeric products of one reaction. Everything else needs a
**balanced reaction**, and `compute_reaction_energy` is the tool for it — it enforces the
balance rather than trusting it, and gives free energies rather than electronic ones. Reach
for it rather than subtracting two unrelated `compute_xtb_energy` results.

## Orderings, not magnitudes

Measured over five textbook isomer pairs, the calculator gets all five **orderings** right and
some **magnitudes** badly wrong — ethanol versus dimethyl ether comes out around 3.5 kcal/mol
against an experimental ~12. So:

- **Report which is more stable.** That is what the method supports.
- **Do not quote the number as the stability difference.** If a magnitude is what the decision
  needs, the honest answer is that a semiempirical single point does not provide it.
- **Treat differences under ~1 kcal/mol as a tie.** That is below the method's resolution on a
  force-field geometry; call them comparable rather than ranking them.

## Two limits that are easy to forget

**One conformer.** Each energy is for a single MMFF-relaxed embedded conformer, not the
molecule's populated ensemble. For anything with rotatable bonds, the difference you compute
may be a difference between two arbitrary conformers rather than between two molecules. The
more flexible the pair, the weaker the comparison — say so explicitly, and reserve real
confidence for rigid systems.

**Not an optimized geometry.** MMFF relaxation is a force field, not GFN2. It is enough to make
the ordering reliable (an unrelaxed geometry is not — it inverted two of those five pairs), but
it is not a stationary point on the surface the energy is computed on. `optimize_geometry` now
fixes that for almost no cost, and `compute_thermochemistry` turns it into a free energy — so
this tool is the cheap screen, and those are what an answer that has to hold up uses.

## Where this sits

Use it to rank isomers, tautomers, or candidate products cheaply, then act on the *ordering*.
When the decision turns on how big the difference is, on a free energy rather than an
electronic energy, or on a temperature dependence, escalate *within* this system first:
`compute_reaction_energy` at `standard` level gives ΔH and ΔG for a balanced comparison, and
`reaction-thermodynamics` holds the judgment for reading them. For a flexible molecule's real
conformational population, or for accuracy beyond semiempirical, neither exists here — say what
experiment would settle it, or escalate (`qm-job-submission`). `computational-evidence` holds
the judgment on when escalating is worth it.
