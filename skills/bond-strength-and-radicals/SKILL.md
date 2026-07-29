---
name: bond-strength-and-radicals
description: >-
  Judgment for computed bond dissociation energies and radical stability — which C-H is
  abstracted, which bond breaks first, how strong an antioxidant is — and the measured
  reason these are a ranking tool and never a number to quote.
tools:
  - compute_reaction_energy
  - predict_site_reactivity
  - compute_electronic_properties
  - find_notes
  - screen_hazards
---

# Bond strengths and radical stability

A bond dissociation energy is the ΔH of a homolysis: `R-H -> R• + H•`. Because
multiplicity is read from the SMILES' own radical electrons, writing `[CH2]c1ccccc1` and
`[H]` is enough to make that a computable reaction — no spin state to declare.

This unlocks the questions autoxidation, HAT chemistry and radical initiation actually
turn on. It also comes with the sharpest accuracy limit in this system, so read the next
section before using any number.

## Measured: rank with these, never quote them

GFN2 is parameterized for geometries, frequencies and non-covalent interactions, not for
breaking bonds into radicals, and it shows:

- Ethane's C–C dissociation is measured at **90 kcal/mol**; computed here it comes out far
  higher — an overestimate of tens of kcal/mol.
- The *ordering* holds. A benzylic C–H comes out clearly weaker than methane's, which is
  correct (measured 89.7 vs 105).
- The *gap* is also overestimated: that ~15 kcal/mol experimental difference comes out
  substantially larger.

So the rules are:

- **Never report a computed BDE as a bond strength.** Not in a report, not in a
  comparison against a literature value, not as "approximately".
- **Rank sites within one molecule**, or closely related molecules, and report the
  ordering. That is what the method supports.
- **Treat the size of a computed gap as an upper bound**, not a magnitude. Two sites
  computed 5 kcal/mol apart are probably closer than that in reality.
- **Very close values are a tie.** If two C–H sites come out within a few kcal/mol, the
  calculation has not separated them and sterics or the reagent will decide.

The result carries an open-shell warning for exactly this reason. Pass it on.

## The questions this answers well

- **Which C–H is abstracted?** Rank the candidate homolyses. Benzylic, allylic, α-to-oxygen
  and tertiary positions come out weak, as they should; primary and aryl C–H come out
  strong. Cross-check against the chemistry: a HAT reagent has its own selectivity, and a
  radical chain's selectivity is set by the abstracting species, not only by the
  substrate.
- **Radical stability.** The same ranking, read the other way — the weakest bond gives the
  most stabilized radical.
- **Antioxidant strength.** A phenolic O–H that is weak relative to the propagating
  radical's is what makes an antioxidant work. Compare candidates against each other.
- **Which bond breaks first on heating.** A ranking of homolyses is a starting point for
  a decomposition hypothesis — but see the safety limit below.
- **Autoxidation liability.** The initiation and propagation steps of autoxidation run on
  C–H abstraction, so a molecule with a notably weak C–H has a real liability worth
  designing a stability study around. `degradation-liabilities` holds that judgment.

## Two things it does not answer

- **Rate and mechanism.** No transition states: a weak bond is not the same as a fast
  abstraction, and a radical chain's kinetics depend on propagation and termination steps
  that are not modelled at all.
- **Whether a radical pathway operates.** These are thermodynamic numbers for a homolysis
  written on paper; whether the real chemistry goes through radicals is a separate
  question that the conditions and the precedent answer.

## The safety limit, which is absolute

A ranking of which bonds are weakest is **never** a thermal-safety assessment. It cannot
say whether a compound decomposes exothermically, at what onset temperature, or with what
energy release — and a decomposition hazard is about all three. It may be used to order a
queue for calorimetry (DSC, ARC) and for nothing else. `safety-screening`'s rule holds
here as everywhere: the screen flags, it never clears.

## Presenting it

Give the ranked sites in chemical language — "the benzylic C–H is the weakest by a clear
margin, then the α-C–H of the ether" — say explicitly that these are orderings and that
the absolute values are not usable, and name what would settle it. Precedent first:
measured BDEs exist for most common motifs, and a literature value beats every number
here.
