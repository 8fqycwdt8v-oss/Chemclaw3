# D-2026-08-01-symmetry-is-an-input-not-a-default — Symmetry is an input, not a default

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** the full-codebase review's
`xtb_thermo` finding · **Extends:** D-2026-08-01-unknown-is-not-fine (a number carries what to
trust about it), D-011 (a result is persisted once and never recomputed)

## Context

Two defects in the same term, found together because the first exposed the second.

**The arithmetic was wrong.** `_rotational`'s linear branch computed the partition function as
`factor * moments[2] / (2 * symmetry)`. The textbook form is `factor * I / sigma`; the `2` is
spurious. Every linear species — N2, O2, CO, CO2, HCN, every alkyne — had its rotational entropy
understated by R·ln2 = 1.377 cal/(mol·K) and its free energy overstated by 0.41 kcal/mol at 298 K.
The nonlinear branch two lines below was correct, which is the cross-check that settles it.

It survived because the module's only entropy-versus-experiment test uses **water**, which is
nonlinear and therefore structurally incapable of seeing it; the CO2 test asserted only
`mode_count == 4`.

**The default was a decision nobody made.** `compute_reaction_energy` constructed `ThermoSpec`
without a symmetry number and offered no parameter through which a caller could supply one, so
every species was computed at sigma = 1. Measured on identical geometries and Hessians, varying
only sigma:

| reaction | sigma = 1 | sigma correct | difference |
|---|---|---|---|
| CO + H2O → CO2 + H2 | −62.540 | −62.130 | **+0.411** = +RT ln 2 |
| C6H6 + H2 → 1,3-cyclohexadiene | +4.694 | +3.222 | **−1.472** = −RT ln 12 |

Half the module's own quoted ±3.0 uncertainty, from bookkeeping, agreeing with RT ln(sigma) to
three decimals.

The module docstring defended the default: *"Within a balanced reaction the error partly cancels;
between unlike species it does not."* That holds only when sigma matches across the arrow, which is
false for every hydrogenation (H2, sigma 2, on one side only) and everything aromatic (benzene,
sigma 12). The defence was load-bearing for `calc.reaction` and it was wrong.

## Decision

**Delete the factor of two.** Pin it with an entropy-versus-literature assertion on a *linear*
molecule, the geometry class the existing test could not reach.

**Take sigma as a per-species input, and withhold ΔG when it is unstated.** Above `level="quick"`,
a species absent from `symmetry_numbers` is computed at sigma = 1 and the reaction returns
`delta_g_kcal=None` with a warning naming those species. ΔE and ΔH are unaffected and still
reported, because sigma enters only the entropy.

**Stating `1` explicitly still yields a ΔG.** "This molecule has no symmetry" and "nobody considered
the symmetry" are different claims, and this is where they part. `SpeciesEnergy.symmetry_number` is
`int | None`, where `None` means *assumed, not known*.

**Invalidate the cache.** Nothing in an `xtb.hess` key moves when *our* arithmetic is corrected, so
every stored linear-species entropy and free energy would have kept being served until tblite
happened to be upgraded for unrelated reasons. That is what `CALCULATION_EPOCH` exists for
(D-2026-08-01-a-key-that-cannot-see-our-own-fix); this fix is the first entry in its log.

## Consequences

- The durable path needed the same parameter. `ReactionJobSpec` and `SolventScreenJobSpec` gained
  `symmetry_numbers` and thread it through, or the Temporal jobs would have silently reported ΔE
  while the inline tool reported ΔG — a gap this decision opened and had to close.
- `delta_g_kcal=None` is not a new code path: it is what `quick` already returns, and both
  consumers already handle it (`activities.py` falls back to the "dE" label, `compare_solvent_effects`
  ranks by ΔE).
- The refusal is narrow. A caller who states sigma loses nothing; a caller who does not loses one
  of three numbers rather than being handed a wrong one.

## Alternatives rejected

- **Derive sigma automatically.** Measured, not assumed: RDKit's graph automorphism counts give
  methane 24 (sigma 12), ammonia 6 (sigma 3), ethane 72 (sigma 6) — the graph overcounts by
  reflections and internal rotors. Sigma is the order of the proper-rotation group of the **3D**
  geometry, which needs point-group detection this layer does not do. A wrong sigma stamped as
  known is strictly worse than an absent one.
- **Report ΔG with a warning.** Rejected on this branch's own precedent: warnings are dropped
  downstream, and `Estimate.render` already establishes that the trust travels on the value line or
  it does not travel (D-2026-08-01-trust-travels-on-the-value-line). A `None` cannot be copied into
  a report; a warned number can.
- **Fix the arithmetic and leave the default.** Would have left the larger error in place — the
  benzene case is 1.47 kcal/mol against the factor-of-two's 0.41 — while making the docstring's
  cancellation defence look freshly validated.
