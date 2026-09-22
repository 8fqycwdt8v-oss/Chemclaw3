# Wave 10 — the module decided, and then asked somebody else

## Items

- [x] **`standardize` argued which fragment is the compound and then asked RDKit** (`771c4837`).
      `core/chem.standardize` opens by arguing a salt has exactly one organic fragment, and then
      called `rdMolStandardize.FragmentParent`, whose chooser counts atoms *including hydrogens*
      and defaults to `preferOrganic=False` — so `[NH4+]` beat formate and ammonium formate
      standardized to **ammonia**. The one organic fragment is now the parent, asked directly.
      `preferOrganic=True` rejected: it substitutes RDKit's "contains a carbon" for `_is_organic`,
      the notion that file argues against.
- [x] **A neutral co-former is not a counterion** (`226d9b88`). The same branch discarded the rest
      on the count alone, so urea hydrogen peroxide became urea. A spectator now earns discarding
      by carrying a charge or by being a solvent RDKit's list knows. Asking the list alone
      regressed TBTU — it is a pharmaceutical salt list and knows no tetrafluoroborate — which is
      why charge leads.
- [x] `STANDARDIZATION_VERSION` -> `std11`, one bump for both halves.
- [ ] Full serial `make cov`, fresh-context subagent review, PR, merge on green CI.

## Measurements this wave rests on

- Re-measured against `origin/main` over **6,481** parseable carbon-bearing SMILES across `data/`,
  `knowledge/`, `src/`, `tests/`, `docs/`, `skills/` and `schema/`: **three** standard forms move —
  ammonium formate `N` -> `O=CO`, and UHP and ethylamine·H2O2 from the stripped fragment back to
  the whole string. **None** of the 68 shipped reagent structures moves.
- `LargestFragmentChooser` on `[NH4+].[O-]C=O` returns `[NH4+]`; with `preferOrganic=True` it
  returns formate. Both driven, and the first is asserted in a test so the measurement is re-run
  rather than remembered.
- `FragmentRemover` over fourteen salts, hydrates and solvates: correct on every one except the
  fluorinated counterions, which it does not carry.

## What the corpus sweep caught that the tests did not

The first spelling of the second half asked `FragmentRemover` alone. Every identity test passed;
the sweep found TBTU had quietly kept its BF4. The behaviour table has no TBTU row — it does now —
and the reagent-table test asserts each reagent note *carries its name*, not that its structure is
unchanged. A sweep over every string in the tree is the only thing that saw it.

## Review

Pending the gate and the subagent review.
