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

| branch | n | span (exp. pKa) | slope | intercept | R² | RMSE | Spearman | worst residual |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| acid (O-H, S-H) | 19 | 0.66 – 15.9 | 0.31221 | −32.98637 | 0.911 | **1.31** | 0.940 | 2.54 (2,2,2-trifluoroethanol) |
| base (aromatic/aryl N) | 12 | 0.72 – 9.11 | 0.32316 | −31.71601 | 0.798 | **1.05** | 0.888 | 2.47 (2-chloropyridine) |

**The result that decides how this should be described: it is not more accurate.** `predict_pka` was
run over the *same* compounds — a comparison that did not previously exist, because its published
±1.6 came from ten acids that are not these nineteen:

| | this composite | `predict_pka` |
| --- | --- | --- |
| 19 acids | RMSE **1.31**, Spearman 0.940 | RMSE **1.34**, Spearman 0.965 |
| 12 aryl-N bases | RMSE **1.05**, Spearman 0.888 | RMSE **1.16**, Spearman 0.944 |

The two are indistinguishable on error, and the fast predictor *ranks better* on both branches. The
comparison is also biased in this composite's favour and still comes out level: its slope and
intercept were fitted **in-sample** on exactly these compounds, while `predict_pka`'s were fitted on
a different set entirely — so the honest reading is that two CREST searches per molecule buy no
accuracy over a cached millisecond lookup.

**Why it ships anyway, stated as narrowly as the measurement allows.** Three things it does that the
fast predictor cannot, none of which is "a better number":

- **It names the proton.** `site_smiles` comes back perceived from the winning geometry —
  `[O-]c1ccccc1`, `Cc1cc[nH+]cc1`, `[NH3+]c1ccc(Cl)cc1`. A pKa without its site is unusable on
  anything polyfunctional, and the rule-based path cannot report one it did not consider.
- **It considers sites no rule offers.** CREST removes every proton in turn, so an imide, a
  sulfonamide or a C-H acid is *ranked* rather than refused — with a warning that the mapping to a
  pKa is then an extrapolation off this calibration's O-H/S-H domain.
- **It reports the microstate count.** 7 protomers of 4-nitroaniline within the search, 5 of
  4-chloroaniline; where more than one lies within RT the molecule has no single conjugate base and
  the macroscopic number is the only one that means anything.

**Two limits the numbers make concrete.** The residual is *class*-structured rather than random —
acetic acid (pKa 4.76) sits at ΔG 125.6 kcal/mol while 4-nitrophenol (7.15) sits at 123.2, an
inversion across families that a single line cannot absorb — and it is not conformational: splitting
the acid residuals by flexibility gives rigid −0.16 against flexible +0.17, so the missing anion
conformational entropy is worth 0.33 units of the 1.31. What is left is the continuum solvent, which
is why `DEFERRED.md` points the next step at explicit solvation rather than at more sampling.

Perception declines rather than guesses, and that is visible in the set: 4-nitroaniline's protomer
came back with no `site_smiles` at all, because bond-order assignment on a delocalised nitro cation
is ambiguous. The warning says so; nothing invents a structure.

## Consequences

- The question "which proton is this pKa about?" is answerable for the first time, and on a
  polyfunctional molecule that is the half of the answer a number cannot carry. What is **not**
  bought is accuracy, and every description of this job says so — the manifest, both skills and the
  result's own warnings — because "the careful pKa" is exactly the phrase a reader would otherwise
  translate into "the more accurate one".
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
