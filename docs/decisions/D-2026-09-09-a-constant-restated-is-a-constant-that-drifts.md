# D-2026-09-09-a-constant-restated-is-a-constant-that-drifts — one definition per physical constant, and a defended damping frequency instead of a false citation

**Status:** accepted · **Date:** 2026-09-09 · **Builds on:**
D-2026-08-16-the-physics-leaves-the-cache-stays (the RRHO arithmetic is what stayed here),
D-132 (`ThermoSettings` is deliberately not part of the Hessian's cache key),
D-2026-08-26-semiempirical-is-the-whole-tier (there is no tier to escalate to, so the tier's own
conventions have to be right), D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit (a claim in
a comment is checked by running it).

## Context

A full audit of this tree's unit arithmetic found **no wrong constant, no sign error and no double
or missing conversion**. The RRHO chain reproduces closed-form references to 1e-13 and matches
NIST-JANAF entropies for H₂O, NH₃, CO₂ and H₂ to within the anharmonicity RRHO omits. This ADR is
therefore not about a defect that reached a chemist. It is about four places where the *guard* would
not have caught one, and one number that had a citation instead of a reason.

### 1. One constant, three definitions, two of them short

`627.5094740631` — one hartree in kcal/mol — was written out three times:

```
science/calc/thermo.py:72   HARTREE_TO_KCAL   = 627.5094740631        (CODATA, exact)
publish/properties.py:89    UNIT_CONVERSIONS  = 627.5094740631        (CODATA, exact)
core/units.py:197           hartree = 2625.4996 kJ/mol  -> 627.509464627151  (rel 1.5e-08 low)
```

and `core/units.py:199` carried `eV = 96.485_332`, truncated from an SI-2019-*exact* 96.48533212331.
CODATA 2018 gives 2625.4996394799 kJ/mol per hartree, so the registry's copy was the one that had
already drifted.

Measured, this reached nobody: `core/units.py` has exactly one production caller
(`connectors/calc/server/tools.py::report_measurement` → `reconcile`, gated on `_CALIBRATED`, whose
two dimensions are `log_solubility` and `acidity`). The energy half of the registry is unreachable
today. **The defect is one caller away**, and the argument for fixing it now is that a copy is what
drifts: three literals agreeing is a fact about one commit, not a property.

### 2. The guard admitted ±0.63 kcal/mol per hartree

`tests/test_units.py` asserted `hartree -> kcal/mol == approx(627.5, rel=1e-3)` — an admissible band
of `[626.8725, 628.1275]`. A whole reaction ΔG fits inside the tolerance of the assertion that
exists to pin the conversion, which is why the registry's 1.5e-08 drift was invisible to it and
would have stayed invisible to a drift eight orders larger. A conversion factor is a *defined*
constant, not a measurement; the right tolerance on it is the tolerance of the arithmetic.

### 3. Two spellings of R in one file, one truncated

`science/calc/thermo.py` held `_GAS_CONSTANT = 8.314462618` beside
`_GAS_CONSTANT_CAL = 1.987204258640832`, which is the *untruncated* `8.31446261815324/4.184`. They
disagreed at rel 1.8e-11. Both `k_B` and `N_A` were already in the file, exact by definition since
the 2019 SI redefinition — so there was a derivation available and two literals were typed instead.

### 4. A damping frequency with a citation that is false in both halves

`core/config/calculators.py:180` said of `xtb_rrho_cutoff_cm = 25.0`: *"25 cm⁻¹ is the published
value and what xtb itself uses."* Checked against both sources it names:

- Grimme 2012 (Chem. Eur. J. **18**, 9955) publishes **ω₀ = 100 cm⁻¹**; downstream implementations
  that follow the paper (Q-Chem, GoodVibes) default to 100 and say so.
- `xtb`'s `--sthr` documentation reads *"rotor cut-off (cm-1) in thermo (default: 50.0)"*.

25 is neither. Measured through this repository's own `_vibrational`, on eight low modes of an
ordinary flexible drug-sized molecule (`[18, 26, 35, 44, 58, 71, 96, 120] cm⁻¹`, 298.15 K):

```
w0 =  25 (shipped)          -T·S = -10.621 kcal/mol
w0 =  50 (xtb --sthr)       -T·S =  -9.531 kcal/mol
w0 = 100 (Grimme 2012)      -T·S =  -8.780 kcal/mol
```

1.09 kcal/mol between the shipped value and xtb's, 1.84 between it and the published one — on the
entropy term of every flexible molecule, below the 3.0 kcal/mol `xtb_reaction_uncertainty_kcal` the
results carry but a third of it, and it does **not** cancel across a reaction that changes
flexibility.

### 5. A key identity claimed by a docstring and not held by the code

`science/calc/calibration.py:10` said predictions are recorded against *"the same
`(calc_type, input_hash)` identity the calculation cache already keys on"*. The ledger hashes the
canonical SMILES; the cache hashes a dict around it. On ethanol: `f29e20f49d416e54` (ledger) against
`a7d334ebee616d78` (cache).

### 6. A coverage figure with no stated target

`Residual.within_uncertainty` is `abs(error) <= sigma` — **±1σ** — and the uncertainties it scores
against are documented 1σ RMSEs (`crippen_logp_uncertainty = 0.68` is Wildman-Crippen's own reported
RMSE). The expected coverage of a correctly calibrated Gaussian error bar is therefore **0.683**,
and `grep` for `0.683|68.3|one sigma` returned nothing anywhere. The only target a reader supplies
unaided is 1.0, against which a perfectly calibrated calculator reads as 32% miscalibrated.

## Decision

**One definition per physical constant, in `core/units.py`, because `core` is the layer everything
may import.** `HARTREE_TO_KCAL`, `JOULE_PER_CALORIE` and `ELECTRONVOLT_TO_KJ` live there at full
CODATA precision; the registry's hartree factor is `HARTREE_TO_KCAL * JOULE_PER_CALORIE` rather
than a third number, and `science/calc/thermo.py` **imports** the name it used to declare.
`_GAS_CONSTANT` is `_BOLTZMANN * _AVOGADRO` and `_GAS_CONSTANT_CAL` is derived from it.

The direction is forced: `tests/test_layering.py` forbids `core` from importing any sibling, so the
definition cannot live in `science/` and be read by the registry. It has to be the other way round.

**`xtb_rrho_cutoff_cm` moves from 25.0 to 50.0** — `xtb`'s own `--sthr` default — and the comment
says that, rather than citing two papers it does not match. The reason is not that 50 is more
published than 100 (it is less): it is that **every Hessian this arithmetic runs on comes out of the
GFN2-xTB tier**, one of the two producers being the `xtb` binary itself, so 50 is the only value
whose correctness a chemist can check. A plain `xtb --ohess` on the same geometry now agrees with
what this system reports. 25 agreed with nothing, and its stated justification was false.

**The three false or missing claims are corrected in place**: the ledger/cache identity, the 0.683
coverage target (module docstring, the `uncertainty_coverage` field and
`Residual.within_uncertainty`), and the qRRHO module docstring, which described a threshold modes
switch at rather than the Head-Gordon 1/(1+(ω₀/ω)⁴) mixture they are actually weighted by.

## What it costs

**A real behavioural change, stated because it is one.** Every flexible molecule's -T·S rises by up
to ~1.1 kcal/mol and its G with it. A reaction's ΔG moves by the difference between its two sides'
low-mode content, which is zero for an isomerization and largest for exactly the ring-closing,
association and conformational-lock chemistry the qRRHO treatment exists for.

**Nothing cached is re-addressed, and that is a property rather than luck.** D-132 made
`ThermoSettings` — temperature, pressure, symmetry number and this cutoff — deliberately *not* part
of the Hessian's cache key, because the second derivatives cannot depend on any of them. The free
energy is recomposed from cached second derivatives on every call, so no `calculation_results` row
carries the old convention and no epoch bump is needed. `CALCULATION_EPOCH` does not move.

What *does* carry the old convention is anything already **published**: `publish/` projects composed
results into a result store this system does not own, and a `ThermochemistryResult` does not report
the cutoff it was computed at, so a delivered record cannot be told apart from a new one. Publishing
is off until `CHEMCLAW_RESULT_SINKS` names a sink, so the shipped configuration has delivered
nothing; a site that has enabled one and wants the distinction should raise it as its own change.

**The measured checks are untouched, verified rather than assumed.** Water, CO₂ and H₂ — the three
NIST comparisons in `tests/test_calc_thermo.py` — have no mode within an order of magnitude of the
damping region; driven at both cutoffs their standard entropies move by ≤ 6e-05 cal/(mol K). Which
is also the finding that made this a guard gap: the suite could not see the cutoff at all, so it
could have been set to any number without turning an assertion red.

## Consequences

- `tests/test_units.py` asserts the energy ladder against CODATA figures written independently of
  the table they check, at `rel=1e-12` instead of `rel=1e-3`, and asserts that the registry's
  hartree factor is *derived from* `HARTREE_TO_KCAL` rather than equal to it by coincidence.
- `tests/test_calc_thermo.py` asserts `thermo.HARTREE_TO_KCAL is units.HARTREE_TO_KCAL` — identity,
  not equality, because equality is exactly what a copy also gives and a copy is what drifted.
- The qRRHO cutoff now has a test: its value, the three -T·S figures above, and the assertion that
  the 25→50 step exceeds a third of the reported error bar. The number's *cost* lives in the test
  rather than in prose, so it cannot go stale the way a transcribed figure does.
- `.env.example` moves with the default, because `tests/test_config.py` compares that file's
  *parsed values* against the code's and fails on any drift. The guard found this change; a
  case-sensitive `grep` for `rrho` had not, because the key is spelled `CHEMCLAW_XTB_RRHO_CUTOFF_CM`.
- `publish/properties.py:89-90` still restates `627.5094740631` and should import
  `HARTREE_TO_KCAL` from `chemclaw.core.units`; `connectors/calc/compose.py` reads the name through
  `science.calc.thermo` and could read it from `core.units` directly. Both are other files' changes
  and are reported rather than made here.
- `connectors/calc/server/tools.py::_record_prediction` carries the same false identity claim this
  ADR corrects in `calibration.py` ("the same identity the calculation cache uses"), and is the
  model-facing half of the 0.683 target. Both belong to that file's owner.
- A `ThermochemistryResult` reports no damping frequency, so a published free energy cannot be
  attributed to a convention. That is a `models.py` change and is left open.
