---
name: charge-tables-and-mass-efficiency
description: >-
  Use when a question turns into "what do I actually weigh out" or "how wasteful is this route" —
  scaling a charge to a basis, expressing a charge in the units it was specified in (molar
  equivalents vs. process volumes), and computing E-factor/PMI so two routes can be compared on
  waste and not only on yield. Load it before writing any charge list, and before quoting a green
  metric, because both fail silently: a mis-expressed solvent charge and an omitted solvent flatter
  the same number in the same direction.
tools:
  - stoichiometry_table
  - green_metrics
  - resolve_compound
---

# Charge tables and mass efficiency

Two tools, one arithmetic. `stoichiometry_table` says what to charge for a batch;
`green_metrics` says what that charge costs in waste. They share a column — every row's
`mass_g` is exactly `green_metrics`' input — so they are used together far more often
than either is used alone.

Neither is a judgment call about chemistry. Both are deterministic arithmetic over
molecular weights and densities, which is precisely why they must not be done in prose:
mental arithmetic over a charge list is where this system has actually been wrong.

## Never compute the charge yourself

The single rule that matters. `stoichiometry_table` takes the basis, the reagents with
their molar equivalents, and the solvents with their volumes, and returns the masses.
Do not multiply equivalents by molecular weights in the answer, and do not convert a
volume charge into equivalents on the way to the tool.

**The incident this rule exists for.** "THF/water 4:1 at 10 volumes" was converted to molar
equivalents by hand and passed as reagents. On a 2 kg basis it put the principal solvent out by a
factor of **2.17** — and the answer then went on to certify the figures as self-consistent, which
they were, because every downstream number was derived from the same wrong mass. A charge table
is checkable only if nothing upstream of it was computed by the model.

## Which path a species goes down

The tool does not police this, and it cannot: only the chemist knows which reading was
meant. Pass each charge **in the units it was specified in**.

- **Specified in equivalents** → `reagents` + `equivalents`. Base at 1.2 equiv, catalyst at
  2 mol% (`0.02`), an oxidant at 3 equiv.
- **Specified in volumes** → `solvents` + `volumes`, where a volume is millilitres per gram of
  basis. "10 volumes of 4:1 THF/water" is `[8.0, 2.0]`, not one entry of 10.

A substance does not belong to one path by its nature. Acetic acid at 1.5 equiv, water in a
hydrolysis, methanol in an esterification, DMSO as the Swern oxidant and DMF as the Vilsmeier
reagent are all charged by equivalent and all also have densities on file. Report which reading
each row used — the returned `role` says so on every row, so quote it rather than assuming.

## Reading the result honestly

- **`unresolved` is not empty-by-default.** A reagent whose name did not resolve carries no
  row and no mass. Name it in the answer as missing; never fill the gap with an estimate, and
  say plainly that every mass metric below is computed without it. `resolve_compound` first is
  the way to find that out before building the table rather than after.
- **A solvent that will not resolve is an error, not an omission**, and so is one with no
  density on file. That asymmetry is deliberate: a chemist scanning a charge list notices a
  missing reagent line, and does not notice a missing solvent — but the solvent usually
  dominates the mass, so its absence would leave a table that looks complete while halving
  every metric derived from it. If the tool refuses, say which solvent and why; the honest
  fallbacks are to charge it by equivalent instead, or to state that the table cannot be built.
- **Solvent moles and equivalents are derived**, not specified. Do not present a solvent's
  equivalents as something the chemist asked for.

## E-factor and PMI

`green_metrics` answers the process-development question yield alone cannot: "comparable
yield at half the PMI" is a real goal and a real reason to change a route.

- E-factor is kg waste per kg product; PMI is kg total input per kg product. They differ by
  exactly 1 by construction, so reporting both as independent evidence is reporting one number
  twice. Lower is better.
- **Include the solvent.** Take the `mass_g` of *every* row, including the `solvent` rows.
  Omitting solvent is the ordinary way these figures get flattered, and it is usually the term
  that dominates them — a reaction run in 10 volumes has more solvent mass than everything else
  combined.
- The tool refuses a mass balance where the inputs total less than the product, rather than
  reporting the negative E-factor that would read as an implausibly green process. If that
  refusal comes back, a reagent or the solvent is missing from the list — fix the list, do not
  work around the check.
- **A mass metric is not a green-chemistry verdict.** It sees mass and nothing else: not
  solvent hazard, not the energy of a cryogenic step, not whether the waste stream is water or
  a chlorinated solvent, not recovery and recycle. Two routes at the same PMI can be very
  different processes. Say which of these the comparison could not see, and load the
  `solvent-selection` and `safety-screening` skills when the answer is about *which* waste
  rather than how much.

## Comparing routes

Build one table per route on the *same* basis and the same product mass, then run
`green_metrics` on each. The comparison between them is far more trustworthy than either
absolute figure, because both carry the same assumptions about what counted as an input.
State the basis explicitly — a PMI quoted without the basis and the isolated yield it was
computed at is not a number anyone can check.
