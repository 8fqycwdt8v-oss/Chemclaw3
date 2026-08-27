# D-2026-08-27-a-free-energy-without-its-standard-state-is-not-a-quantity — a solution ΔG is quoted at 1 mol/L, derived from the phase rather than configured

## Status

Accepted. Changes published numbers: every solution-phase ΔG for a reaction that changes the
molecule count moves by 1.894·Δn kcal/mol. `D-2026-08-16-the-physics-leaves-the-cache-stays` stands
— this is the arithmetic that stayed here, corrected, not a boundary being redrawn.

## Context

`science/calc/thermo.py` computes an RRHO free energy from a Hessian the calculation server took.
The electronic energy comes back from an **ALPB implicit-solvent** SCF, but the entropy and thermal
corrections were the ideal-gas ones at `xtb_thermo_pressure_pa = 101325` — 1 atm — in every phase.
`reaction_energy` accepts any *atom*-balanced equation (`check_balance` counts atoms and charge,
never molecules), so associations, dissociations and homolyses all pass, and their ΔG was a
**gas-phase 1 atm** number presented as a solution free energy with nothing saying so.

The standard state a chemist means in solution is 1 mol/L. For an ideal gas
`G(P) = G°(P°) + RT ln(P/P°)`, and the pressure of one mole per litre is `c0·R·T`, so the two
conventions differ by

```
RT ln(RT c0 / P0) = 8.314462618 · 298.15 · ln(1000 · 8.314462618 · 298.15 / 101325) / 4184
                  = 1.8943284454483122 kcal/mol   per mole of species
```

independent of mass, because it is purely the volume factor of the translational partition
function. Derived a second way — the Sackur–Tetrode entropy difference at the two pressures, as
`-T·ΔS` — it agrees to 1e-12 for masses from 2 to 342 amu. In an equilibrium constant that is a
factor of **24.4654 per unit of Δn**.

A Diels–Alder in THF (Δn = −1) reported at −5.00 kcal/mol is −6.89 at 1 M: `K = 4.6e3` against
`K = 1.1e5`. A homolysis (Δn = +1) is wrong the other way, and so is every host–guest association.
Tautomer, protomer and stereoisomer rankings (`species_ranking`) are untouched, because Δn = 0
there and the term cancels exactly — which is why this never surfaced as an obviously wrong number.

What made it indefensible rather than merely approximate is the contrast inside one function: the
same code path **withholds ΔG entirely** when a rotational symmetry number is unstated, over an
`R ln σ` term worth 0.41 kcal/mol at σ=2, while a term four and a half times larger was silent. A
grep over `src/`, `docs/decisions/` and `skills/` for `1.89`, `24.4` and `standard state` returned
nothing.

## Decision

**The standard state follows the phase, and is not a knob.** `thermochemistry_from_hessian` reads
`HessianPayload.solvent` — which it already reports — and evaluates the translational partition
function at 1 atm in vacuum and at `c0·R·T` in solution. `standard_state_for` is the one definition
of the rule; `ThermoSettings.pressure_pa` narrows to the gas-phase reference it always was.

A knob was rejected for the reason this repository has named repeatedly: it would have been a knob
nobody sets, and it would have let one composite quote a species at 1 atm while another quoted the
same species in the same solvent at 1 M. Deriving it from the payload also makes the correction
impossible to apply twice and impossible to forget — no composite computes Δn, and the term cancels
by arithmetic wherever it should.

**The state travels with the number.** `ThermochemistryResult`, `ReactionEnergyResult` and
`SolventEffect` each carry `standard_state`, `publish/` projects it beside every ΔG it writes, and
`skills/reaction-thermodynamics` tells the model to quote it for exactly the reactions where it
does not cancel. `solvent_comparison` warns when Δn ≠ 0 that its gas reference is at 1 atm while
its solvent rows are at 1 M, so the gas-to-solution gap is not read as a solvation energy;
solvent-against-solvent, which is what that screen ranks, is like against like and unaffected.

**What is *not* corrected**, because it is not a free energy at that standard state:
`InteractionResult.interaction_energy_kcal` is an electronic energy difference (its model already
says the association entropy is absent), enthalpies and electronic energies do not depend on the
reference pressure at all, and the unimolecular composites — conformer refinement, rotation
profiles, transition-state barriers — are Δn = 0 and so are numerically untouched while still
being labelled correctly.

## Three smaller findings of the same shape, in the same pass

- **`LogdResult.uncertainty` was a copy, not a propagation.** `dlogD/dpKa` is the ionised
  fraction, and `_require_a_single_equilibrium` refuses anything above
  `logd_negligible_ionised_fraction`, so the surviving population is skewed to *small* fractions:
  pyridine at pH 7.4 published ±1.4 of which the pKa contributes 0.0094, while Crippen's own
  ~0.68 RMSE — the term that actually dominates there — was omitted entirely. It is now
  `hypot(crippen_logp_uncertainty, f_ionised · σ_pKa)`, which moves pyridine down to 0.680 and
  benzoic acid (93 % ionised) up to 1.636. Both docstrings that asserted the old behaviour are
  corrected rather than left.
- **`bond_dissociation_survey` fabricated σ = 1** for the parent and both fragments, which marked
  it *stated* and disarmed the withhold-and-warn machinery `species_ranking` documents having fixed
  one composite over. Harmless only because the survey reads ΔH and discards the ΔG — benzene's
  C–H would have been wrong by `RT ln(2/12) = −1.06 kcal/mol` as a BDFE. The argument is deleted and
  the composed reaction's warnings are surfaced instead of swallowed.
- **Two dead public functions are deleted.** `structural_domain` (with `_ORGANIC_ELEMENTS`) was a
  line-for-line duplicate of the live check in `Chemclaw3-mcp`'s `calc` server, with no caller in
  `src/` and kept green by tests that called it directly — the `map_to_hpc_identity` shape, and a
  reviewer reading it would have concluded the control was here. `barrier_from_half_life` had no
  caller either. An absence test now fails whoever re-adds the first without a producer.

`CalculationKey.build` was audited alongside them and is **kept**. It is dead in `src/` — every
`calc` key comes back from the server and is assembled by `connectors.calc.remote.remote_key` — but
it is the one definition of the `CALCULATION_EPOCH` fold that the suite can exercise, and deleting
it would hand-fold the epoch across the seven test files that construct keys, leaving the rule in
the tests and `src/` with none. Its docstring stops claiming it is "the only honest way to make a
key" and names the live path instead. Giving it a live caller — the other branch the audit named —
means unifying the two folds, which cannot be done without changing one of the two hash payloads
and cold-missing every cached row; that is a separate decision.

## Consequences

- Stored `ReactionEnergyResult` and `ThermochemistryResult` rows written before today carry no
  `standard_state` and validate back as `gas-1atm`, which is truthfully what they were.
- `crippen_logp_uncertainty` joins `config/calculators.py` and `.env.example`.
- The config comment claiming 1 atm is "the reference state every tabulated thermodynamic quantity
  is quoted at" is corrected: the thermochemical standard has been 1 bar since 1982, and the
  solution standard is 1 mol/L.
- `tests/test_calc_thermo.py` derives the correction from CODATA constants twice — the ideal-gas
  relation and the Sackur–Tetrode difference — rather than from anything in `src/`, so the code
  under test cannot also produce the expectation.
