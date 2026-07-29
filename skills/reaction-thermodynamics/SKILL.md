---
name: reaction-thermodynamics
description: >-
  Judgment for computed reaction energies — what a ΔG says (where the equilibrium sits),
  what it never says (whether the reaction goes at a useful rate), how to write an
  equation the calculation can actually answer, and when the number is inside the noise.
tools:
  - compute_reaction_energy
  - compute_thermochemistry
  - get_durable_job_status
  - find_notes
  - gather_evidence
---

# Reaction thermodynamics

`compute_reaction_energy` is the first tool here whose output can go into a report. That
makes it the one most worth being careful with.

## The equation is the hard part

Write it before you compute it, and write it *balanced* — the tool refuses an unbalanced
one, and the refusal is usually telling you something. A forgotten water in a
condensation, a proton that appears from nowhere in an acid-catalysed step, a counterion
on one side only: each is a real gap in the mechanism you were about to reason about.

Two rules that decide whether the answer means anything:

- **List each species once per equivalent.** Two waters is `["O", "O"]`. A missing
  equivalent is a whole molecule's worth of error.
- **Charges must balance, and bare ions are the weak spot.** A calculated free energy for
  a naked proton or hydroxide in solution is not a physical quantity at this level of
  theory. Prefer an equation where charge is *carried across* (an acid and its conjugate
  base on opposite sides) over one where an ion is created or destroyed. If you cannot,
  say the number is indicative only.

## What ΔG answers, and the question it does not

ΔG places the **equilibrium**. Negative means products are favoured if the reaction is
allowed to reach equilibrium; roughly, −1.4 kcal/mol is a factor of ten in K at room
temperature.

It says **nothing about rate**. There are no transition states in this system: a strongly
downhill reaction may be completely inert at room temperature, and a barely favourable
one may be instant. Every "will this reaction go?" question is really two questions, and
this tool answers only one of them. Say which one you answered.

Related traps:

- **A reaction that is uphill can still be run**, by removing a product (distilling water
  out of an esterification), by using an excess, or by coupling it to something downhill.
  Report the equilibrium, then discuss how a process changes it.
- **ΔH versus ΔG.** If the entropy term is doing the work — a reaction that changes the
  number of molecules — say so, because that is the part temperature will move.
- **Temperature is an input.** The default is 298.15 K. A process at 80 °C is a different
  question, and re-running at the real temperature costs nothing.

## Read the uncertainty before the number

Every result carries one, and at this level of theory it is a few kcal/mol. Three
consequences worth stating out loud:

- **A ΔG of −1.5 with a ±3 uncertainty has not established the direction.** Say the
  reaction is near thermoneutral, not that it is favourable.
- **Comparing two similar reactions is far more reliable than either absolute value**,
  because the errors largely cancel. Prefer "this substrate is ~4 kcal/mol more
  favourable than that one" to two separate absolute claims.
- **`warnings` is not decoration.** A species reported as not a minimum has a free energy
  that is not a free energy; an open-shell warning means the numbers are ranking-only
  (see `bond-strength-and-radicals`). Pass these on rather than filtering them out.

## One conformer, always

`conformer_treatment` says `single` on every result, and it is the most common silent
error in semiempirical work. Each species is one relaxed conformer, not its populated
ensemble. For rigid species this barely matters; for a flexible chain, an amide, or
anything with several plausible shapes, the ΔG could move by more than the effect you are
looking for. `conformational-analysis` holds the judgment on when this invalidates an
answer rather than merely widening it.

## When it comes back as a job id

Large requests return a job id instead of a result — a reaction over several sizeable
species, or a solvent screen. That is not an error: tell the user it is running, give
them the id, and check it with `get_durable_job_status`. Do not resubmit while waiting; an
identical request returns the same job rather than starting a second one.

## Precedent still outranks it

`find_notes` first. A recorded outcome for this transformation contains the catalyst, the
solvent, the temperature and the workup that no calculated ΔG does. Use the calculation
where precedent is silent, or to explain a result precedent already gave you.
