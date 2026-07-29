---
name: degradation-liabilities
description: >-
  Judgment for "where will this molecule fall apart?" — identifying likely oxidation and
  hydrolysis sites, turning them into a forced-degradation study design, and generating
  plausible impurity structures for an unknown peak without over-claiming.
tools:
  - predict_site_reactivity
  - compute_electronic_properties
  - compute_reaction_energy
  - predict_pka
  - find_notes
  - gather_evidence
---

# Degradation liabilities

Stability work asks two questions: *before* the study, "which conditions and which sites should
I be looking at?", and *after* it, "what is this unknown peak?". Computed site reactivity helps
with both — as a way to focus attention, never as a prediction of what will be found.

## Designing a forced-degradation study

The point of a forced-degradation study is to find the degradants that matter under conditions
harsh enough to produce them and mild enough that they are relevant. Calculation contributes
one thing: a prior on **where** the molecule is vulnerable, so the analysis knows what to look
for and the conditions can be chosen to provoke it.

- **Oxidation.** `predict_site_reactivity` in `electrophilic` mode ranks the electron-rich
  sites an oxidant attacks; a high HOMO (`compute_electronic_properties`) says the molecule is
  globally easy to oxidize. For abstraction chemistry specifically — peroxides, autoxidation,
  a radical initiator — rank the C-H homolyses instead (`bond-strength-and-radicals`), because
  a weak C-H and an electron-rich site are not the same liability. Classic liabilities to check the ranking against: benzylic and
  allylic positions, ethers and their α-C–H, amines (especially tertiary), thioethers,
  electron-rich arenes, aldehydes.
- **Hydrolysis.** Not a Fukui question — it is a functional-group question. Esters, amides,
  carbamates, lactones, lactams, imines, nitriles, and epoxides are the checklist; partial
  charges can indicate how electrophilic a given carbonyl is, which helps *rank* several
  candidates within one molecule. `predict_pka` matters here because ionization changes both
  the rate and the mechanism — but read `ionization-and-partitioning` first, and remember it
  refuses aliphatic amines (aromatic nitrogen it does cover).
- **Photolysis and thermal degradation.** Largely outside what these tools describe. Say so and
  point at the study rather than guessing.

**Precedent first, as always.** `find_notes` for the compound class: a known degradation
pathway in the ELN or the literature outranks any descriptor.

## Hypothesizing a structure for an unknown peak

Given a mass and a retention time, the usual candidates are an oxidation (+16, +32), a
hydrolysis fragment, a dimer, a dealkylation, or an isomer. Computation cannot identify the
peak — it can *filter* the list:

- Does the proposed modification sit at a site the molecule actually exposes? A hydroxylation
  at a position the Fukui ranking puts last is a weaker hypothesis than one at the top.
- Does a candidate structure's **computed IR spectrum** match the measured one? Where an
  isomeric assignment is the question, this is often the only computable discriminator —
  `computed-spectra-comparison` holds how far to trust it.
- Is the proposed structure even reachable from the parent under the conditions used?
- If several positions are plausible, say so rather than picking the top-ranked one — the
  descriptor cannot distinguish isomeric hydroxylation products with confidence.

Present these as hypotheses to test analytically, never as an identification. The analytical
data identifies the peak; the calculation only decides which hypotheses are worth the LC-MS
time.

## Hard limits

- **This is not a stability prediction.** It says where a molecule is electronically
  vulnerable, not whether it will degrade, how fast, or under what storage conditions. Never
  turn a ranking into a shelf-life statement or a "this compound is stable" claim.
- **Not a substitute for the study.** ICH forced-degradation and stability studies exist
  because prediction is not adequate; this focuses them, it does not replace them.
- **Formulation is invisible.** Excipient interactions, trace metals, peroxide impurities, and
  moisture drive a great deal of real degradation and appear nowhere in the calculation.
- **Solid state is invisible.** Everything here is a molecular argument; crystal packing,
  amorphous content and surface effects are not modelled.
- **Autoxidation chain chemistry, only in part.** Real autoxidation runs through radical
  propagation, whose key quantity is a bond dissociation energy. Those are now computable
  (`compute_reaction_energy` over a homolysis), and `bond-strength-and-radicals` holds the
  judgment — including the measured reason they rank sites and must never be quoted as bond
  strengths. What is still absent is the chain itself: initiation, propagation and termination
  rates, and therefore whether a weak C-H actually becomes a degradation pathway.

## Presenting it

Give the ranked sites in chemical language — "the benzylic position and the tertiary amine are
the two electron-rich handles" — with the conditions that probe each, and state plainly that
this is a prior for study design rather than a prediction of outcome. If a stability claim is
what the user needs, say the study is what provides it.
