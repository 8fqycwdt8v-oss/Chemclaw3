# D-2026-08-02-a-solvent-charge-is-a-volume — A solvent charge is a volume

**Status:** accepted · **Date:** 2026-08-02 · **Extends:** D-2026-08-02-a-probe-is-a-question-you-have-not-asked-yet

## Context

`stoichiometry_table` accepted a basis and a list of `(reagent, molar equivalent)` pairs. A chemist
does not charge solvent that way — a solvent charge is *volumes*, millilitres per gram of basis.
There was no way to express one, so in the live run "10 volumes of THF" was passed in as 10 molar
equivalents. On a 2 kg basis that put the principal solvent out by a factor of **2.17**, and the
answer then certified the figures as self-consistent, which they were: the arithmetic was right
about the wrong quantity.

The damage does not stop at the charge table. `green_metrics`' own docstring points at
"`stoichiometry_table`, whose `mass_g` column is exactly this input", and warns that "omitting
solvent is the usual way these numbers get flattered". Solvent mass dominates E-factor and PMI. So
the one pairing the tool documents was broken on the term that dominates both outputs.

## Decision

**Solvents are charged by volume, in their own `solvents`/`volumes` arguments, and a known solvent
passed as a reagent is rejected outright.** Not accepted-as-written, not silently converted: the
molar-equivalent path is what produced the 2.17× error, and a table that quietly accepts a solvent
there gives no sign of which reading was meant. The error message names the fix.

**Solvents share `ChargeRow` rather than living in a second list.** A separate list would invite
the model to hand `green_metrics` the reagent masses alone — precisely how those metrics get
flattered. Every row carries a real `mass_g` and real `moles_mmol` however the charge was
expressed, so "take the `mass_g` of every row" is a complete and correct instruction. A new `role`
field (`basis` / `reagent` / `solvent`) says which quantity the chemist actually specified, so a
reader can see what was given and what was derived; solvent moles and equivalents are always
derived.

**An unresolvable solvent, or one with no density on file, is an error — not an `unresolved`
entry.** This is asymmetric with reagents on purpose. A chemist reads a charge list line by line
and *sees* a missing reagent; a missing solvent leaves a table that looks complete while quietly
halving every mass metric computed from it. Neither a zero nor a guessed 1 g/mL is an acceptable
stand-in for a density.

Densities live on the ~15 solvents already in `core/reagents.py`, beside the names and SMILES they
belong to, rather than in a new table.

## Consequences

- The `stoichiometry_table` → `green_metrics` pairing works as its docstring has always described.
- The rejection is a **behaviour change** for any caller that was passing a solvent as a reagent.
  That is the intent: such a call was producing a wrong number, and there is no reading under which
  it was right.
- A solvent outside `core/reagents.py` cannot be charged by volume until its density is added. The
  error says so and offers charging it by mass as a reagent, which is correct and merely less
  convenient — better than a table that is confidently wrong.
