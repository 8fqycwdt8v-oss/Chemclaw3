---
name: reactivity-descriptors
description: >-
  Judgment for reading GFN2-xTB electronic descriptors — Fukui site rankings,
  HOMO/LUMO gaps, partial charges, bond orders — to answer regioselectivity and
  reactivity questions without over-claiming what a semiempirical number supports.
tools:
  - predict_site_reactivity
  - compute_electronic_properties
---

# Reactivity descriptors

Holds the *judgment* for interpreting the electronic descriptors; the mechanics live
in `predict_site_reactivity` and `compute_electronic_properties`. Use this to turn a
ranking into an answer a chemist can act on — and to say honestly what it does not
support.

## Which descriptor for which question

| Question | Reach for |
|---|---|
| "Which position is nitrated/halogenated/attacked by an electrophile?" | `predict_site_reactivity`, mode `electrophilic` |
| "Where does a nucleophile add?" | `predict_site_reactivity`, mode `nucleophilic` |
| "Which C-H is abstracted by a radical?" | `predict_site_reactivity`, mode `radical` |
| "Which of these is more easily oxidized / more conjugated?" | `compute_electronic_properties`, compare HOMO / gap |
| "Where does the electron density sit? Is this bond polarized?" | `compute_electronic_properties`, partial charges |
| "Is this bond single, double, or delocalized?" | `compute_electronic_properties`, Wiberg bond orders |

## Reading a site ranking

**Compare like with like.** The ranking covers every atom, and a heteroatom usually
tops it because of its lone pair — that is a real result about the lone pair, not
about ring substitution. For "which ring position reacts", compare the **ring carbons
with each other** and ignore the heteroatom and hydrogens.

**Expect the classical pattern, and say when you do not get it.** Electron-donating
substituents (-OH, -OR, -NR₂, alkyl) should rank *ortho* and *para* above *meta*;
electron-withdrawing ones (-NO₂, -CN, -C(=O)R, -SO₂R) should invert that to *meta*.
If the calculation disagrees with the classical expectation, do not quietly report the
calculation — say both, and say which you trust and why.

**Gaps between sites matter more than their values.** A clear separation between the
top site and the rest is a usable prediction; a cluster within a few thousandths is a
tie, and the honest answer is "these positions are electronically comparable, so
sterics or the reagent will decide".

## What these numbers do not support

- **No cross-molecule comparison.** Fukui indices are normalized per molecule (each
  function sums to 1), so a value of 0.09 in one molecule and 0.09 in another say
  nothing about which molecule is more reactive. Use them to rank *within* a structure.
- **Electronics only.** Sterics, the specific reagent, the solvent, temperature, and
  catalyst are not in the model. A site can be electronically preferred and
  practically inaccessible.
- **No rates, no yields, no selectivity ratios.** The output is an ordering, i.e. a
  hypothesis worth testing — never a predicted product distribution.
- **A force-field geometry.** These run on an MMFF-relaxed embedded conformer, not a
  GFN2-optimized one, and on a single conformer. For a flexible molecule whose
  conformation changes which face or site is exposed, treat the ranking as indicative.
- **Frontier orbital energies are not IPs or EAs.** Use HOMO/LUMO/gap to *compare*
  related molecules; do not quote one as an ionization potential or a redox potential.

## Presenting the result

State the mode you ranked for and why it matches the chemistry asked about, name the
top sites by their position in the molecule (*para* carbon, carbonyl carbon) rather
than by bare atom index, and give the caveat that fits the question — usually the
electronics-only one. If the decision needs more than an ordering, say what would
settle it: an experiment, or the heavier QM path (`submit_qm_job`).
