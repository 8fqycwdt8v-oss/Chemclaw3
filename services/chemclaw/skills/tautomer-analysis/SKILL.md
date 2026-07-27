---
name: tautomer-analysis
description: >-
  Judgment for "which structure is this molecule actually in?" — deciding when a tautomer
  question needs asking at all, reading a computed tautomer ranking honestly, and knowing
  that getting it wrong silently invalidates every other number about the compound.
tools:
  - sample_conformers
  - compute_reaction_energy
  - predict_pka
  - compute_electronic_properties
  - find_notes
  - gather_evidence
---

# Tautomer analysis

The question that comes *before* the others. A pKa, a Fukui ranking, a dipole, a reaction
free energy, a computed IR spectrum — every one of them describes whichever tautomer was
drawn in the SMILES. If that is not the dominant form, the number is not wrong by a
little; it is a number about a different molecule.

## When to ask

Ask whenever the structure contains a group that can move a proton between heteroatoms:

- **amide / imidic acid** — amides are overwhelmingly amide, so this one usually resolves
  itself, but say so rather than skipping it;
- **keto / enol** — simple ketones are keto; 1,3-dicarbonyls, and anything where the enol
  is conjugated or intramolecularly hydrogen-bonded, genuinely are not;
- **heterocyclic N–H** — pyrazoles, imidazoles, triazoles, tetrazoles, purines. This is
  the big one in pharma, and the ratio is frequently close;
- **2-pyridone / 2-hydroxypyridine**, **amidine**, **guanidine**, **thioamide**;
- anything **aromatic in one form and not the other**, where the energy gap is large and
  the answer is usually obvious once asked.

Skip it for a molecule with no mobile proton. Saying "no tautomerism here" costs a
sentence and tells the reader you checked.

## Reading the ranking

`sample_conformers` with `search="tautomers"` enumerates and ranks them. What the ranking
supports:

- **A large gap (> ~3 kcal/mol) is a real answer.** One form dominates; use it for
  everything downstream and say which one.
- **A small gap is also an answer, and a more important one**: both forms are present,
  and any property that differs between them is not a single number. Report the mixture
  rather than picking the lower one.
- **The ordering is more reliable than the gap.** Semiempirical tautomer energies carry
  the same few-kcal/mol uncertainty as every other energy here.

## Four ways this goes wrong

- **The solvent decides, and often flips the answer.** Tautomer equilibria are strongly
  medium-dependent — 2-hydroxypyridine dominates in the gas phase, 2-pyridone in water.
  Always run it in the solvent that matters, and never quote a gas-phase tautomer ratio
  for a solution-phase question.
- **The search is stochastic and enumeration is not guaranteed.** A form that was not
  sampled cannot be ranked. If a tautomer you expect is missing from the list, that is a
  reason to look harder, not evidence it does not exist.
- **Crystalline and solution forms can differ.** The tautomer in the solid state is a
  packing question this cannot answer.
- **It is one more calculation on one more approximate surface.** A measured answer — NMR,
  X-ray, or a literature study of the same scaffold — outranks it. `find_notes` first.

## What to do with the answer

State the dominant tautomer explicitly, then *use it consistently*: recompute the pKa,
the descriptors, the reaction energy on that form rather than on whatever was drawn. When
two forms are close, either report both sets of numbers or say plainly that the property
is not single-valued. The failure this skill exists to prevent is a confident answer
computed on a minor tautomer, and it is invisible unless someone asks.
