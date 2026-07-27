---
name: computed-spectra-comparison
description: >-
  Judgment for using a computed IR spectrum to discriminate between candidate structures
  — which features are trustworthy (band pattern and relative intensity), which are not
  (absolute positions), and how to rank hypotheses for an unknown impurity without
  claiming an identification.
tools:
  - compute_thermochemistry
  - compute_electronic_properties
  - find_notes
  - gather_evidence
---

# Comparing a computed spectrum with a measured one

A recurring, genuinely painful analytical problem: an unknown peak, a mass, and three
candidate structures that all fit it. `compute_thermochemistry` returns vibrational
frequencies **with IR intensities**, i.e. a computable spectrum — which turns "all three
are plausible" into a ranking, provided you compare the right things.

## What is trustworthy, and what is not

- **Absolute band positions are not.** Harmonic semiempirical frequencies are
  systematically off, typically several percent high, and the error is not uniform across
  mode types. Never match a computed wavenumber to a measured one and call it a hit.
- **The pattern is.** Which functional groups produce strong bands, roughly where, and in
  what order — a carbonyl well above a C–O stretch, an O–H or N–H at the top of the
  range, a strong band where a candidate structure predicts one and a bare region where
  it does not.
- **Relative intensities are, as an ordering.** The strongest band being the carbonyl, or
  a candidate predicting an intense band in a region the measured spectrum shows as
  empty, is real evidence. The absolute km/mol values are not quantitative.
- **Differences between candidates are the most reliable of all**, because the systematic
  error is largely shared. Compare the *candidates against each other*, then ask which
  comparison the measured spectrum matches.

A practical consequence: if two candidates differ only in a region where all their
predicted bands are weak, the spectrum does not distinguish them. Say so.

## How to run the comparison

1. **Enumerate candidates explicitly** — every structure consistent with the mass and the
   chemistry. A ranking is only as good as the list.
2. **Compute each one the same way**: same solvent setting, same temperature, and the
   same band cut-off. A comparison across different settings is not a comparison.
3. **Check `is_minimum` first.** A candidate whose geometry is a saddle point has a
   spectrum containing an imaginary mode; its frequencies are not reliable and it should
   be re-examined, not silently ranked.
4. **Compare the strong bands**, region by region, and write down what each candidate
   predicts that the others do not. That distinguishing feature is the whole argument.
5. **Rank, and say how far apart the candidates are.** "Two of the three predict a strong
   band at ~1700 that the sample does not show" is a real result; "candidate B matches
   best" without saying why is not.

## Hard limits

- **This is not an identification.** NMR, high-resolution MS and an authentic standard
  identify a compound. A spectral comparison narrows the list and says which authentic
  standard is worth making — which is often the expensive step it saves.
- **Gas-phase, one conformer, harmonic.** No solvent shifts on band positions, no
  hydrogen-bonding broadening, no overtones or combination bands, no Fermi resonance, and
  no conformational averaging. Regions dominated by hydrogen bonding (a carboxylic acid
  O–H, an amide N–H) are where this is least reliable.
- **No other spectroscopy.** NMR shifts and UV-Vis are not computed here; only IR.
- **Molecular, not solid state.** A measured spectrum of a crystalline sample carries
  packing effects that are not in the calculation.

## Presenting it

Give the ranking with the distinguishing feature for each candidate, state that positions
are indicative and patterns are the evidence, and finish with what would settle it.
`find_notes` first: a known degradant or impurity for this compound class outranks any
computed spectrum, and `degradation-liabilities` holds the judgment on which candidate
structures are chemically reachable in the first place — an electronically plausible
structure that the parent cannot actually form is not a candidate.
