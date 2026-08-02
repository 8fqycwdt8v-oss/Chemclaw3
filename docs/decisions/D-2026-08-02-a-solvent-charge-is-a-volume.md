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

**A charge expressed in volumes goes in `solvents`/`volumes`, which did not exist before.** That is
the whole of the fix. The tool converts, so nobody converts by hand, which is where the 2.17× came
from.

**The tool does not police which path a substance takes, and a first version of this decision that
did was wrong.** It rejected any reagent `density_of` could answer for, on the reasoning that
having a density means being a solvent. It does not. Ten entries in the density table are routinely
charged by molar equivalent *as reagents* — acetic acid at 1.5 equiv, water in a hydrolysis,
methanol in an esterification, DMSO as the Swern oxidant, DMF as the Vilsmeier reagent — so the
rejection broke correct calls, and because `density_of` resolves SMILES too there was no spelling
left that could charge three equivalents of water at all. Having a density is a fact about a
substance; being charged by volume is a fact about one experiment, and only the chemist knows which
they meant. The row's `role` reports which path was taken rather than the tool choosing it.

That first version also shipped with a test that hid it: the existing offload test was rewritten
from `("CCO", 46.0, ["water"], [1.0])` to the solvent path, silently turning 1.0 equivalent
(18.0 g) into 2.552 equivalents (45.91 g). The test asserted only thread identity, so it passed. **A
test edited to accommodate a change is evidence about the change**, and this one was the evidence.

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
- Every existing call keeps working. The two new arguments default to empty, so this is purely
  additive — which is what it should have been from the start.
- A substance outside `core/reagents.py`'s density table cannot be charged by volume until its
  density is added. The error says so and offers converting to molar equivalents by hand — less
  convenient, and better than a table that is confidently wrong.
- **What this does *not* prevent** is the original error itself: nothing stops a caller passing
  "10 volumes" as 10 equivalents, because 10 equivalents of THF is also a legal charge and the tool
  cannot see the chemist's intent. The volumes path removes the *need* to convert by hand, and the
  docstring says which argument a volume belongs in. Making it impossible was the tempting version
  and it cost more than it bought.
