# D-2026-08-26-a-pka-is-a-macrostate-not-a-microstate — the ensemble pKa is a composite here, beside the fast predictor rather than replacing it

**Status:** accepted · **Date:** 2026-08-26 · Depends on
`D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual`, which made the CREST searches this
composite is built on actually run. Does **not** supersede `predict_pka`.

## Context

`predict_pka` (now `Chemclaw3-mcp`'s `servers/calc/engine/pka.py`) answers in milliseconds from a
cache and is calibrated: O-H/S-H acids at ~1.6 pKa units, aromatic/aryl nitrogen bases at ±1.0. Two
structural facts about it bound what it can do, and neither is a bug:

- **The sites are enumerated by rule.** `_acidic_protons` takes O-H and S-H; `_basic_nitrogens`
  applies four hand-written exclusions (amide, nitrile, pyrrole-type, sulfonamide). A site no rule
  offers is not considered, and the rules encode a chemist's expectation of where the proton is.
- **One conformer per species.** Both sides are a seeded embedding, so a molecule whose anion is
  stabilised by a fold the neutral cannot make is described by whichever geometry ETKDG produced.

CREST answers both differently: `--deprotonate` removes **every** proton in turn, optimises each
product and ranks them by GFN2 energy, and a conformer search samples the neutral. With the binary
now shipping, that pipeline is available — and the question is what to do with it.

## Decision

**A new composite, `connectors/calc/compose.py::microstate_pka`, exposed as the durable
`predict_pka_ensemble` job. `predict_pka` is untouched.**

Three parts of that are deliberate.

**It is a composite here, not a tool on the server.** Its key would have to name the microstate the
deprotonation search settles on, which is an output — `D-2026-08-16-the-physics-leaves-the-cache-stays`'s
structural giveaway that a calculation is a loop with state. Both halves underneath are ordinary
keyed primitives, so the expensive parts cache separately: the same molecule's conformer ensemble
asked for on its own is a hit, and a second pKa at another temperature is arithmetic.

**It is a second calculator, not a level on the first.** The two disagree by construction — phenol's
most stable protomer is the ring-protonated arenium ion, which no rule in `pka.py` would offer — so
they keep separate calibrations and separate ledger histories, and a disagreement between them is
information. Folding this in as `level="thorough"` would also have put a two-CREST-search cost
behind an argument on a sub-second cached tool, which is the shape that gets set by accident.

**The free energy is the macrostate's, summed over microstates.** `-RT ln Σ gᵢ exp(-Eᵢ/RT)` over
everything each side found, not the best member of it. This is an identity rather than a refinement:
an equilibrium constant between two states made of interconverting microstates *is* the ratio of
their partition functions. Two deprotonation sites within RT both carry population, and calling
either one "the conjugate base" is wrong by up to RT ln 2 — 0.41 kcal/mol, about 0.3 pKa units
through the fitted slope. `macrostate_free_energy_kcal` sits beside `ensemble_correction_kcal`
(`-T·S_conf` added to the lowest member, the standard thermochemical treatment) and the two are
documented against each other precisely because they look interchangeable and are not.

## The calibration, measured

Fitted through this exact pipeline — conformer search of the neutral, microstate search of the
ionised form, macrostate free energies, both in ALPB water at 298.15 K, `effort="quick"`, crest
3.0.2 / xtb 6.7.1 / tblite. Reference values are standard aqueous pKa at 25 °C.

<!-- MEASURED-RESULTS -->

## Consequences

- A pKa that has to be right has a route that does not depend on a rule having offered the right
  site. The site it used is reported (`site_smiles`, perceived from the winning geometry) and is
  frequently the interesting half of the answer.
- **The aliphatic-amine limit survives**, and that is worth being explicit about: CREST fixes the
  *enumeration*, and the aliphatic-amine failure is the *solvent model*. Over 13 reference amines
  the computed basicity correlates with experiment at Spearman -0.17, because aqueous aliphatic
  amine basicity is set by the ammonium ion's hydrogen bonding to water, which a continuum cannot
  represent. Better sampling produces a better-sampled number with no ranking information. This
  composite warns rather than refusing (the server's `predict_pka` refuses), because it reports the
  free energy and the microstates alongside — those are real, the mapping to a pKa is what is not.
- The reported number carries warnings for every case where the arithmetic succeeds and means less
  than it looks like it does: a carbanion winner, a site that could not be perceived, several
  microstates within RT, a solvent other than the fitted one, and a pKa outside the fitted span.
- `calc_max_primitive_calls` counts both searches before either starts, so the ceiling is reached
  before the expensive half is paid rather than after.

## The rule

**A calibration is a property of the pipeline that produced it, not of the property it predicts.**
Two ways of computing a deprotonation free energy need two calibrations, two ledger histories and
two `calc_version`s — and the honest way to add a better method is to put it beside the old one,
not to move the old one's slope. Every number in the section above was measured through the code
that reads it; a refit is another measurement, never a tuning.
