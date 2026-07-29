---
name: atropisomer-assessment
description: >-
  Judgment for hindered rotation with a regulatory consequence — turning a computed
  rotational barrier into an interconversion half-life, deciding which ICH class a
  compound falls in, and being clear that a computed barrier informs that call without
  settling it.
tools:
  - scan_coordinate
  - compute_thermochemistry
  - find_notes
  - gather_evidence
---

# Atropisomer assessment

One of the few questions in this system where a computed number maps onto a regulatory
decision. A biaryl (or amide, or other hindered single bond) whose rotation is slow
enough produces separable, interconverting stereoisomers — and if they interconvert
slowly enough, they are separate entities that must be controlled as such.

## The physical chain

A rotational barrier ΔG‡ implies a rate, and the rate implies a half-life:

    k = (kB·T/h) · exp(−ΔG‡ / RT)      t½ = ln2 / k

At 25 °C the useful anchors are roughly: **20 kcal/mol → seconds**, **24 → minutes to
an hour**, **27 → about a day**, **30 → a few years**. Note how steep this is — one
kcal/mol is a factor of ~5 in half-life, and the method's uncertainty is larger than one
kcal/mol. That is the central limitation and it belongs in the answer.

The conventional classification (ICH M1/Q6-style reasoning, and the widely used LaPlante
scheme) sorts compounds by that half-life: **freely rotating** (barrier below ~20
kcal/mol, a single compound), **an intermediate class** where rotation is slow on a
laboratory timescale but not indefinitely, and **atropisomers proper** (above ~30
kcal/mol, isolable stereoisomers that must be controlled and specified separately). The
consequences differ by class, so this is a decision worth getting right.

## How to compute it

1. **Identify the bond.** Usually the biaryl axis or the amide C–N. Get the four atom
   indices that define the dihedral across it.
2. **Scan the torsion** with `scan_coordinate`, covering the full rotation. Two things to
   check: that the profile is smooth, and that the maximum is resolved rather than
   stepped over — rescan the barrier region finely.
3. **Read the barrier** as the highest point relative to the populated minimum.
4. **Convert to a half-life** with the relation above, and report the class it implies.

A relaxed scan is a reasonable route to a torsional barrier — this is the case where the
reaction coordinate genuinely *is* the one angle being driven. That is not true of most
barriers, and `conformational-analysis` says why.

## The honesty this skill exists to enforce

- **The method's error is bigger than the decision boundary.** A computed 26 kcal/mol
  with a few kcal/mol of uncertainty spans "hours" to "years" and therefore spans two
  classes. Give the number, give the range it implies, and say which classifications
  remain live. Never present a class as determined by the calculation.
- **A computed barrier is not a measurement.** Variable-temperature NMR, chiral HPLC with
  interconversion studies, and racemization kinetics are what establish this. The
  calculation says whether those experiments are worth running and roughly what to
  expect — which is genuinely useful and is not the same thing.
- **Substituents near the axis decide it, and sterics are where this method is weakest.**
  Treat an *ortho*-substituted biaryl's barrier as indicative.
- **Solvent and temperature move it.** The process temperature, not 25 °C, is what
  matters for whether the compound racemizes during manufacture — and a compound can be
  configurationally stable on the shelf and completely labile at reflux.
- **One conformer, one coordinate.** A molecule with a second hindered axis, or one where
  rotation is coupled to a ring flip, is not described by a single torsion scan.

## Presenting it

Lead with the barrier and the half-life it implies, both with their uncertainty, then the
classification range rather than a single class. State explicitly what experiment would
settle it. `find_notes` first, always: a measured barrier for a close analogue outranks
any calculation here, and biaryl series are exactly where such data tends to exist.
