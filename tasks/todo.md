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

- Re-measured against `origin/main` over every token in the tree that parses as a multi-fragment
  carbon-bearing SMILES: **seven** standard forms move. The first sweep tokenised on quotes alone,
  said three, and missed the whitespace-delimited strings in `data/evals/probes/platform.yaml` —
  acetone·H2O2 and the NaH/DMF·H2O2 route, which are the **safety probes**, where the old pipeline
  discarded the peroxide out of a question about peroxides. **None** of the 68 distinct structures
  in the shipped reagent table moves.
- `LargestFragmentChooser` on `[NH4+].[O-]C=O` returns `[NH4+]`; with `preferOrganic=True` it
  returns formate. Both driven, and the first is asserted in a test so the measurement is re-run
  rather than remembered.
- `FragmentRemover` carries hexafluorophosphate and **not** tetrafluoroborate, so the list-only
  spelling regressed TBTU and TSTU while HATU and PyBOP were fine. Pinned in
  `tests/test_upstream_surface.py`, because after this change that catalogue decides whether a
  hydrate collapses and nothing else would red if upstream edited it.

## What the corpus sweep caught that the tests did not

The first spelling of the second half asked `FragmentRemover` alone. Every identity test passed;
the sweep found TBTU had quietly kept its BF4. The behaviour table has no TBTU row — it does now —
and the reagent-table test asserts each reagent note *carries its name*, not that its structure is
unchanged. A sweep over every string in the tree is the only thing that saw it.

## Review

The reviewer's sweep found what mine did not, twice over, and both misses were in how I *looked*
rather than in the code. My tokenizer used quotes; the safety probes are whitespace-delimited YAML
scalars, so the two most consequential movers were invisible to it. And I claimed
`FragmentRemover` knew neither fluorinated counterion when it carries PF6 — a half-measurement
stated as a whole one, in five places including the test docstring whose purpose is to hold it.

The one real defect was `all(...)` over the spectators, which coupled them: a single unrecognised
neutral preserved every other fragment, so a peroxide in the string kept a chloride. Asked per
spectator now, with both cases asserted.

The trade the change makes is named rather than discovered later: for a counterion RDKit's
catalogue omits, a salt written neutral and the same salt written ionic get two `compound_id`s.
Measured, that set is seven acids; `Reionizer` does not close it; it is taken against four wrong
identities that ship today (UHP is urea, BH3·THF is THF, BH3·SMe2 is dimethyl sulfide,
DABCO·2H2O2 is DABCO), and it carries its own row.
