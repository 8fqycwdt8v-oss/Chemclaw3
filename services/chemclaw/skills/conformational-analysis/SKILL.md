---
name: conformational-analysis
description: >-
  Judgment for molecular shape — reading a torsion profile, deciding when one conformer
  is enough and when it silently invalidates every other number in an answer, and knowing
  what a relaxed scan can and cannot see.
tools:
  - scan_coordinate
  - optimize_geometry
  - compute_thermochemistry
  - compute_xtb_energy
---

# Conformational analysis

Two jobs. The first is answering shape questions directly — which rotamer is populated,
how high is a rotational barrier, how much strain a ring closure costs. The second is
quieter and matters more: **deciding whether the single conformer under every other
calculation is good enough**, because when it is not, nothing downstream is right.

## The standing caveat, stated once

Every number this system produces — an energy, a pKa, a dipole, a Fukui ranking, a
reaction ΔG — describes **one** conformer: whatever geometry was embedded and relaxed.
There is no ensemble and no Boltzmann average anywhere yet.

When that is fine:

- rigid molecules — aromatics, fused rings, small or highly substituted systems;
- comparisons between close analogues, where the same conformational error appears on
  both sides and largely cancels;
- questions about *which site* rather than *how much* — a Fukui ranking on a ring is
  robust to how a distant chain happens to be folded.

When it is not, and you should say so plainly:

- flexible chains with several rotatable bonds, where the populated shape is a
  distribution rather than a structure;
- anything where an intramolecular hydrogen bond may or may not be formed — the two
  conformers can differ by several kcal/mol, which is larger than most effects being
  asked about;
- entropy and free energy for a floppy molecule, where the conformational contribution is
  missing entirely;
- any comparison between a rigid species and a flexible one, where the error does *not*
  cancel and systematically favours the rigid side.

The honest phrasing is not "this may be inaccurate". It is: *this describes one shape of
a molecule that has many, and the number could move by more than the effect you are
looking for.*

## Reading a torsion profile

`scan_coordinate` with four atoms drives a dihedral while everything else relaxes. What
to take from it:

- **The minima are the populated rotamers**, and their relative energies say roughly how
  populated. A gap under ~1 kcal/mol means both are present in real amounts; above ~3,
  the lower one dominates.
- **The maxima are barriers to interconversion**, and their height is what decides
  whether "conformers" or "isomers" is the right word — see `atropisomer-assessment`.
- **Resolution matters.** A coarse scan can step straight over a minimum. If a profile
  looks featureless, rescan the interesting range more finely before concluding anything.
- **Symmetry is a free check.** A profile that should be symmetric about 180° and is not
  means a point relaxed into a different basin, and that point is not comparable with its
  neighbours.

## What a relaxed scan cannot do

- **It is not a transition-state search.** The maximum of a profile approximates a
  barrier for a torsion, where the reaction coordinate really is that one angle. For a
  bond being broken or formed it is a sketch — treat it as an upper bound with a wide
  error bar, and never quote it as an activation energy.
- **The scanned atoms are frozen**, so their own local geometry cannot relax with the
  coordinate. Fine for torsions; a real limitation when the constraint fights the
  chemistry.
- **Each point starts from the input geometry**, deliberately, so the profile does not
  depend on which direction it was walked. The cost is that a point can relax into a
  different basin than its neighbours — visible as a discontinuity, which is worth
  investigating rather than smoothing over.
- **One coordinate at a time.** Two coupled torsions need a 2D surface this tool does not
  produce, and a molecule whose shape is set by two dihedrals together is not answered by
  scanning either one.

## Finding a better conformer

There is no conformer search here (it arrives with plan X6). What you can do:

- Scan the rotatable bond you suspect, take `minimum_structure`, and note that it is
  lower than what you started with — evidence that the original number described a
  strained shape.
- `optimize_geometry` reports `relaxation_kcal`. A large value means the starting
  geometry was poor, which is itself worth reporting.
- For a molecule with several rotatable bonds, say plainly that a proper conformer search
  is what the question needs and that this system does not do one yet.

## Presenting it

Give the populated shapes in chemical language — "the anti rotamer dominates, with the
gauche about 0.7 kcal/mol above it, so both are present" — and attach the barrier when
interconversion matters. Where the single-conformer limit undermines the answer, lead
with that rather than burying it.
