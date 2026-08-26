---
name: reactivity-descriptors
description: >-
  Judgment for reading GFN2-xTB electronic descriptors — Fukui site rankings, the
  conceptual-DFT panel, partial charges, bond orders, polarisabilities — to answer
  regioselectivity and reactivity questions by *position*, with an error bar, without
  over-claiming what a semiempirical number supports.
tools:
  - describe_sites
  - predict_site_reactivity
  - compute_electronic_properties
  - compute_atomic_descriptors
  - compute_surface_potential
---

# Reactivity descriptors

Holds the *judgment* for interpreting the electronic descriptors; the mechanics live in the tools.
Use this to turn a ranking into an answer a chemist can act on — and to say honestly what it does
not support.

## Always start with `describe_sites`

It is free, it runs no calculation, and **skipping it is how the right number becomes the wrong
answer.** It gives one entry per symmetry class carrying the position a chemist reads ("the para
aromatic carbon"), the `scopes` the site answers, the hydrogens that belong to it, and a `site_id`
that survives the molecule being rewritten.

Never work out atom indices yourself from a SMILES. Never report a bare index.

The measured reason: `predict_site_reactivity` on phenol gets the chemistry right — *para* 0.0845,
*ortho* 0.0471, *meta* 0.0381 across the ring carbons — and in the returned ranking the *para*
carbon sits **6th of 13**, behind the hydroxyl oxygen and four hydrogens, with both *meta* carbons
below four hydrogens. `top_n` does not fix it: the default is 15 and the molecule has 13 atoms.

## Then scope the question, don't lengthen the list

| The question | Scope to | Descriptor |
|---|---|---|
| Which position is nitrated / halogenated / attacked by an electrophile? | `ring_carbons` | f⁻ |
| Where does a nucleophile add? Which S<sub>N</sub>Ar site goes first? | `ring_carbons`, `electrophilic_carbons` | f⁺ |
| Which C–H does a radical abstract? Where is the metabolic soft spot? | `ch_sites` | f⁰ |
| Which of these two carbonyls reacts first? | `electrophilic_carbons` | f⁺, local electrophilicity |
| Which end of an ambident nucleophile attacks? | `heteroatoms` | local softness s⁻ |
| Where does this oxidise? | `heteroatoms`, `ch_sites` | f⁻, HOMO |
| Which atom is polarisable / makes a halogen bond? | `heteroatoms` | polarisability (`compute_atomic_descriptors`) |
| Is there a sigma-hole? Where is the surface most positive? | — | `compute_surface_potential` |
| Is this bond single, double or delocalised? | — | Wiberg bond orders |

For an azine, `adjacent_ring_heteroatoms` is often the fact that decides it: in
2,4-dichloropyrimidine both C–Cl carbons are *ortho* to a ring nitrogen and only one sits **between
two**, which is what makes it the more electrophilic.

## Aggregate by symmetry class, and use the spread as the error bar

Atoms in one `describe_sites` class are the same atom. **Average their indices and take the spread
(max − min) as this calculation's own noise floor** — it costs nothing, because those atoms were
already computed.

Then the rule: **report a difference between two classes only when it exceeds the spread within
them.**

Measured, this is not a formality. Toluene's ring classes agree to 0.0000. Phenol's *ortho* class
splits by **0.0088** — its O–H is planar, so one *ortho* carbon is *syn* and the other *anti* — and
the *ortho*-to-*meta* difference is **0.0090**. So for phenol the honest answer is:

> *Para* is the clear electrophilic site (f⁻ 0.085 against 0.047 and 0.038). *Ortho* and *meta*
> differ by 0.009, which is inside this calculation's own 0.009 spread over chemically equivalent
> positions — it does not resolve them.

Reporting "C6 *ortho* beats C2 *ortho*" instead would be reporting an O–H rotamer as chemistry.

## The global panel, and what it is for

Every site ranking now carries `descriptors`: IP, EA, chemical potential μ, hardness η, softness S,
electrophilicity ω. They come free from the three single points the Fukui calculation already runs.

- **Use them to order a series**, and to scale the local indices.
- **Never quote one as a measurement.** These are vertical ΔSCF values from a semiempirical
  Hamiltonian: GFN2 puts phenol's IP at 13.5 eV against an experimental 8.5.
- **`local_electrophilicity_ev` (ω·f⁺) is the one index carrying a global scale factor**, so it is
  the only one with any chance of comparing sites *across* molecules — which is what a covalent
  warhead question needs. That claim is not settled. Say so when you use it, and prefer
  `report_measurement` and `calculator_trust` over asserting it.

## What these numbers do not support

- **No cross-molecule Fukui comparison.** Each Fukui function sums to 1 by construction, so 0.09 in
  one molecule and 0.09 in another say nothing about which molecule is more reactive. The
  polarisability panel from `compute_atomic_descriptors` is the exception — it is not normalised.
- **Electronics only.** Sterics, the specific reagent, the solvent, temperature and catalyst are not
  in the model. A site can be electronically preferred and practically inaccessible.
- **No rates, no yields, no selectivity ratios.** The output is an ordering with an error bar, i.e.
  a hypothesis worth testing — never a predicted product distribution.
- **A force-field geometry, and one conformer.** The class spread exposes conformer noise; it does
  not remove it. For a flexible molecule whose conformation changes which face is exposed, run
  `sample_conformers` first and rank at a named `structure_id`.
- **Frontier orbital energies are not IPs or EAs.** Use HOMO/LUMO/gap to *compare* related
  molecules.
- **`free_valence` is absent for sulfur, phosphorus and iodine**, and that is correct rather than
  missing: they have more than one normal valence, so there is no single number to subtract from.

## Expect the classical pattern, and say when you do not get it

Electron-donating substituents (–OH, –OR, –NR₂, alkyl) should rank *ortho* and *para* above *meta*;
electron-withdrawing ones (–NO₂, –CN, –C(=O)R, –SO₂R) should invert that. If the calculation
disagrees with the classical expectation, do not quietly report the calculation — say both, say
which you trust and why, and check whether the difference survives the class spread first.

## Presenting the result

Name the sites by their `label`, never by index — `describe_sites` guarantees the labels are unique
within a molecule, so one always identifies one site. State the mode you ranked for and why it
matches the chemistry asked about. Give the class mean and the spread together, and say which
comparisons the spread resolved and which it did not. "These two positions are not resolved by this
calculation" is a real answer, and far more often the honest one than it is comfortable.

**No tool returns a "resolved" flag** — you compute it, from the class means and the spread, by the
rule above. Do not report one as if a calculator had decided it.

For a ring-substitution answer, pass the winning class's `atoms` to `render_structure`'s
`highlight_atoms` so the chemist can check the position at a glance.

If the decision needs more than an ordering, say what would settle it: an experiment, or the heavier
QM path (`compute_dft_energy`).
