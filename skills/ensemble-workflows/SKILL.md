---
name: ensemble-workflows
description: >-
  Judgment for multi-step calculations over sets — when a conformer ensemble or a species
  set changes an answer rather than merely widening it, what free-energy weighting costs
  against electronic weighting, and how to read a population that came from a sample.
tools:
  - sample_conformers
  - refine_ensemble
  - compute_ensemble_property
  - rank_species
  - survey_bond_strengths
  - enumerate_tautomers
  - enumerate_protonation_states
  - enumerate_stereoisomers
  - enumerate_bond_cleavages
  - describe_topology
  - compute_reaction_energy
  - compute_thermochemistry
tags:
  - computation
---

# Ensemble and species workflows

Every other calculator here answers about **one structure**. These answer about a *set* — the
shapes a molecule takes, or the forms it exists as — and the judgment they need is different in
kind, not just in degree. Two rules govern all of it:

**A set is the universe of its answer.** A conformer that was not sampled is not reported as
absent; a tautomer that was not enumerated is not in the distribution. Both come back with the
counts that make this checkable, and both are worth a sentence in the answer.

**A population is a sampled, approximate quantity twice over** — once because the search is
metadynamics rather than enumeration, once because the energies ranking it carry several kcal/mol
of error. Populations of 60/40 and 55/45 are the same answer.

## Which workflow

| The question | Reach for |
|---|---|
| Which form is this molecule *in*? | `run_tautomer_resolution` — before anything else about it |
| What is charged, at which pH? | `run_microspecies_profile` (amphoteric/polyprotic); `predict_pka` for one site |
| How much of the folded form is there? | `run_ensemble_free_energy` |
| Is this dipole/gap/charge a real number for a floppy molecule? | `run_regioselectivity_in_conformer` for sites, `compute_ensemble_property` otherwise |
| Which bond breaks first? | `run_bond_strength_survey` |
| Which diastereomer is favoured? | `run_stereoisomer_ranking` |
| What could this have degraded into? | `run_degradant_triage` |

**Ask `describe_topology` first when you are not sure it is worth it.** It is free and structural:
few rotatable bonds means a conformer search will find little, one ionisable site means
`predict_pka` covers the question, no unassigned stereocentre means an expansion returns one
structure. Spending a search to discover the molecule was rigid is the commonest waste here.

## Enumerate, then compute — and never the reverse

The enumeration tools (`enumerate_tautomers`, `enumerate_protonation_states`,
`enumerate_stereoisomers`, `enumerate_bond_cleavages`) are **free**: pure graph operations, no
calculation, no cache. The ranking jobs that take their output are the expensive half.

That split is why they are separate tools rather than one. It means you can look at the species set
— how many forms, which sites — and decide whether to spend anything at all. It also means the set
is *visible in the answer*, so a reader can see which universe the distribution is over.

Do not ask a ranking job to enumerate. It will not; it ranks what it is given, and a form you did
not pass was not considered.

## Electronic weighting versus free-energy weighting

`sample_conformers` weights by **electronic energy**. That assumes every conformer has the same
zero-point, thermal and entropic contribution, and for the molecules a search is worth running on
that assumption fails in a specific direction: a compact, hydrogen-bonded fold has both a low
electronic energy *and* a stiff, ordered set of low-frequency modes, so E-weighting over-populates
it.

`refine_ensemble` weights by **free energy**, at the cost of one optimization and one Hessian per
member — minutes each on a drug-sized molecule.

**It is a different treatment, not a better one.** Say which produced the numbers you report. And
read `refined_population_covered`: refining the five lowest of forty-seven and calling the result
"the ensemble" is the error the truncation warning exists to prevent, and it is worse than the
equivalent error on electronic energies because a free energy looks more careful.

Reach for the refinement when a *population* is the answer. When a *geometry* is the answer —
something to run the next calculation on — `sample_conformers` already gives you
`lowest_structure_id` for a fraction of the cost.

## Reading an averaged property

`compute_ensemble_property` returns a mean **and a spread**. The spread is the finding as often as
the mean is.

When the values scatter across the ensemble by more than the difference you are using the number to
argue — comparing two analogues whose dipoles differ by 0.3 D, over an ensemble spanning 1.5 D —
the honest report is that the molecule does not have one value of this property at this
temperature. Reporting the mean alone turns that into a false precision that reads exactly like a
measurement.

For per-atom properties (`fukui`, `charges`) the same rule applies atom by atom: where one atom's
index varies as much as the gap to the next atom, there is no ranking to report.

## What these cost, and what the budget refusal means

A conformer search is the most expensive single calculation in this system — measured, 47 s for
14-atom n-butane and 1142 s for 33-atom ibuprofen, at the cheapest effort. The fan-out jobs
multiply it: `rank_species` at `level="thorough"` is one search *per species*.

They therefore count their calculations before starting and refuse above a configured ceiling,
naming the count. That refusal is not a failure to report as one — it is the system telling you the
question is bigger than it looks. The response is to narrow the species set, drop the level, or say
to the chemist what the full version would cost. Do not retry it unchanged.

`level` on `rank_species` is the main lever: `quick` ranks by electronic energy and says so in a
warning, `standard` adds a Hessian per species for a free energy, `thorough` adds a conformer search
per species. Most tautomer questions are answered at `standard`.

## The tautomer question comes first

It has the widest blast radius of anything here. A pKa, a Fukui ranking, a dipole and a reaction
free energy all describe whichever tautomer was drawn in the SMILES. If that is the minor form,
none of those numbers is wrong by a little — each is a number about a different molecule, and
nothing in the output says so.

Ask it whenever the structure has a proton that can move between heteroatoms: heterocyclic N-H
(pyrazoles, imidazoles, triazoles, purines — the big one in pharma), 1,3-dicarbonyls, amidines,
guanidines, thioamides, 2-pyridone/2-hydroxypyridine. Skip it for a molecule with no mobile proton,
and say you checked — that costs a sentence and tells the reader the question was asked.

**A tautomer ranking that does not search conformers can be qualitatively wrong, not just imprecise.**
Measured: acetylacetone ranked from one embedding per tautomer at GFN2 comes out **99.9% keto**,
against roughly 80% *enol* in the gas phase. The enol is stabilised by an intramolecular hydrogen
bond that exists in one planar conformer and in none of the others, so a ranking that never looks
for that conformer cannot see the thing that makes the enol favourable — and inverts the textbook
case while looking entirely reasonable. `run_tautomer_resolution` therefore ranks at
`level="thorough"`, a conformer search per tautomer, which is the most expensive default here and
the right one. If you rank at a cheaper level, say so in the answer.

Reading the ranking: a gap above about 3 kcal/mol is a real answer and everything downstream should
use that form. A **small gap is also an answer, and a more important one** — both forms are
populated, and any property that differs between them is not a single number. Report the mixture
rather than picking the lower one.

## Chaining these yourself

Two handles carry work between steps, and both are arguments rather than prose:

- **A geometry** is a `structure_id`. `sample_conformers` reports one per conformer plus
  `lowest_structure_id`; `optimize_geometry`, `compute_thermochemistry`,
  `compute_electronic_properties`, `predict_site_reactivity` and `scan_coordinate` all take one.
  Passing the SMILES on instead re-embeds the molecule and throws the search away.
- **A species set** is a list of SMILES. The enumeration tools produce one; `rank_species` and
  `survey_bond_strengths` take one.

When a named workflow above fits, use it — the order is fixed there because getting it wrong is
silent. When the question is genuinely novel, chain the tools yourself with those two handles, and
say which sequence you ran so the answer can be reproduced.
