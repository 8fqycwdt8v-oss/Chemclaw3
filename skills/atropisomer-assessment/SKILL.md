---
name: atropisomer-assessment
description: >-
  Judgment for hindered rotation with a regulatory consequence — turning a computed
  rotational barrier into an interconversion half-life, deciding which ICH class a
  compound falls in, and being clear that a computed barrier informs that call without
  settling it.
tools:
  - enumerate_torsions
  - profile_rotation
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

At 25 °C the anchors are: **20 kcal/mol → 51 s**, **24 → 12 hours**, **27 → 80 days**,
**30 → 35 years**. Note how steep this is — one kcal/mol is a factor of ~5 in half-life,
and the method's uncertainty is larger than one kcal/mol. That is the central limitation
and it belongs in the answer.

**Those four numbers are computed, not remembered.** `profile_rotation` does this
arithmetic — Eyring, in the calculation layer rather than in prose — and reports the
half-life with the band the uncertainty implies. The table above replaced a prose one that read
"27 → about a day" and "30 → a few years" — wrong by nearly two orders of magnitude at
the top of exactly the range where the classification boundary sits. Do not work a
half-life out by hand; read it off the result.

The conventional classification (ICH M1/Q6-style reasoning, and the widely used LaPlante
scheme) sorts compounds by that half-life: **freely rotating** (barrier below ~20
kcal/mol, a single compound), **an intermediate class** where rotation is slow on a
laboratory timescale but not indefinitely, and **atropisomers proper** (above ~30
kcal/mol, isolable stereoisomers that must be controlled and specified separately). The
consequences differ by class, so this is a decision worth getting right.

## How to compute it

1. **Name the bond with `enumerate_torsions`** — usually the biaryl axis or the amide C–N.
   Pass its entry through unchanged. **Never work the atom indices out yourself.** An
   index is not a name: the same pair names an amide C–N in one way of writing a compound
   and an aromatic *ring* bond in another, both in range, both really bonded — so a
   hand-assembled torsion returns a plausible barrier for a different bond.
2. **Say back which bond you chose**, by its label, before spending anything. If the
   chemist's words match more than one entry, ask rather than picking. `render_structure`
   with the torsion's `atoms` draws it, which is the one form a human can check at a
   glance.
3. **Run `profile_rotation`.** It covers one period rather than a full turn where the
   torsion is symmetric, resolves each maximum instead of stepping over it, releases each
   well from its constraint into a real rotamer, and reports the barrier out of each well
   with the half-life and its band already computed.
4. **Report the class range** from that band, never from the middle value.

A relaxed profile is a reasonable route to a torsional barrier — this is the case where
the reaction coordinate genuinely *is* the one angle being driven. That is not true of
most barriers, and `conformational-analysis` says why.

`scan_coordinate` still drives any internal coordinate and is right when the *profile* is
the question. `profile_rotation` is right when the *barrier* is: everything in steps 3
and 4 is what it adds.

## The honesty this skill exists to enforce

- **The method's error is bigger than the decision boundary.** A computed 26 kcal/mol at
  ±3 spans **2.2 hours to 6.4 years** — two classes — and the result carries both ends as
  `half_life_seconds_fastest` and `half_life_seconds_slowest`. Quote the range and say
  which classifications remain live. Never present a class as determined by the
  calculation, and never quote the middle value alone: on its own it reads exactly like a
  measurement.
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
  rotation is coupled to a ring flip, is not described by a single torsion profile, and
  **nothing checks that for you** — `enumerate_torsions` lists the other rotatable bonds,
  so look at what it returned before treating one profile as the answer. Pass a
  `structure_id` from `sample_conformers` when which conformer the barrier is measured in
  could matter.

## Presenting it

Lead with the barrier and the half-life it implies, both with their uncertainty, then the
classification range rather than a single class. State explicitly what experiment would
settle it. `find_notes` first, always: a measured barrier for a close analogue outranks
any calculation here, and biaryl series are exactly where such data tends to exist.
