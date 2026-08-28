# D-2026-08-27-a-solvate-is-not-its-solvent — A solvate is not its solvent

**Status:** accepted · **Date:** 2026-08-27 · **Refines:**
`D-2026-08-01-a-reagent-is-not-its-largest-fragment` (the counterion rule, whose "Consequences"
section left this open by name) · **Closes:** the `BACKLOG.md` row *"A solvate collapses onto
whichever fragment is larger"*

## Context

`core/chem.py::standardize` answers "is this the same compound?", and `FragmentParent` is the step
that discards counterions. D-2026-08-01 put a gate in front of it for the three species where
discarding the smaller fragments deletes the compound — a wholly inorganic reagent, a metal complex,
an organometallic — and closed with a fourth it did not address:

> **Still open, deliberately.** `standard_smiles("CCN.C1CCOC1")` → THF: a solvate collapses onto
> whichever fragment is larger, since both are organic.

Measured on this tree before the change:

| input | `standard_smiles` | |
|---|---|---|
| `CCN.C1CCOC1` (ethylamine/THF solvate) | `C1CCOC1` | **THF** |
| `C1CCOC1` (neat THF) | `C1CCOC1` | the same string |

and therefore `compound_id("CCN.C1CCOC1") == compound_id("C1CCOC1")` — `True`. That is worse than
a cache miss, because `compound_id` is the knowledge-graph note id: a solvated compound and its
solvent reach one note, one body and one fingerprint row.

**The tie-break is not merely wrong, it is unstable.** `FragmentParent` picks by molecular weight,
so which fragment survives is a property of the *pair*. Measured: `Cc1ccccc1O.C1CCOC1` (cresol in
THF) standardized to the cresol, while `Cc1ccccc1O.Cc1ccccc1` (the same solute in toluene)
standardized to **toluene** — swapping the solvent silently changed which substance the record was
about.

### The blast radius, measured before deciding

- The D-011 calculation cache is **unaffected**: it keys on `require_canonical_smiles`, which is
  spelling-only and never runs this pipeline.
- Both fingerprint indices have a designed invalidation lever — `STANDARDIZATION_VERSION` rides in
  `molecule_definition()` and `reaction_definition()`, so rows indexed under an older notion of
  sameness fall out of similarity search rather than being ranked against newer ones.
- `core/reagents.py::_RAW_SYNONYMS`: 97 spellings over **68** distinct structures, 21 of them
  multi-fragment. **0 of the 68 change** under this decision.
- `data/vendored/records.csv`: 35 rows, 5 multi-fragment, none affected.
- The committed corpus holds **9** compound notes, **0** multi-fragment.

### What the measurement turned up, and why it shapes the fix

Three shipped reagents — LDA, HATU and TBTU — already collapse today
(`CC(C)[N-]C(C)C.[Li+]` → `CC(C)NC(C)C`, i.e. LDA recorded as diisopropylamine), and
`records.csv`'s sodium *tert*-butoxide already becomes *tert*-butanol. **That is D-2026-08-01's
counterion rule working as designed, not this defect.** Any fix here has to leave all four in
place, or it is a re-litigation of that decision wearing a solvate's clothes.

## Decision

**Strip fragments only when exactly one of them is organic.** One organic fragment is a salt: the
rest are counterions or waters of crystallization, and discarding them is what the pipeline is for.
**Two or more is a solvate, a co-crystal or an organic-acid salt, and the structure names no
winner** — so the species is kept whole. Zero remains D-2026-08-01's wholly-inorganic case.

`_identity_survives_stripping` is replaced by `_metal_is_the_compound` (its two metal checks, each
still asked of the stage that holds its evidence) plus a count of organic fragments read in
`standardize`, because the count and the metal tests no longer gate the same set of operations.

`Uncharger` is gated on the same count, one step looser: it runs whenever *some* fragment is
organic, including on the kept-whole solvate. That is load-bearing, not incidental — see the
reverted variant below.

`STANDARDIZATION_VERSION` moves `std5` → `std6`. It is consumed in exactly three places — both
fingerprint `definition` strings and `ingest/labels/labeller.py`'s version stamp — and the bump is
warranted because this changes *what the pipeline collapses*, which is the constant's stated
trigger. A deployment's existing rows for any multi-fragment organic species were indexed under an
identity this build no longer agrees with; the bump makes them invisible to search until a re-index
rebuilds them, rather than ranking them against new ones.

## Consequences, measured after the change

| claim | before | after |
|---|---|---|
| `compound_id("CCN.C1CCOC1") == compound_id("C1CCOC1")` | `True` | `False` |
| `standard_smiles("CCN.C1CCOC1")` | `C1CCOC1` | `C1CCOC1.CCN` |
| shipped reagents changed, of 68 distinct | — | **0** |
| shipped reagents the pipeline shrinks at all | 3 (LDA, HATU, TBTU) | **3, the same three** |
| `records.csv` sodium *tert*-butoxide | `CC(C)(C)O` | `CC(C)(C)O` |

`tests/test_compound_identity.py` pins each: the solvate no longer collapses, the tie-break no
longer depends on which solvent it is, and — as an **absence** test, so that nobody "fixes" the
salt behaviour later — exactly `{lithium diisopropylamide, HATU, TBTU}` lose a fragment.

**The caveat, stated rather than assumed.** The rule keeps nicotine bitartrate whole
(`CN1CCC[C@H]1c1cccnc1.O=C(O)[C@H](O)[C@@H](O)C(=O)O`), and a tartrate is arguably a counterion
rather than a solvent; caffeine citrate is the same shape. Nothing in the *structure* separates that
from an ethylamine/THF solvate — both are two organic fragments — so a rule that split them would
have to be a rule about which molecules are solvents, and this is deliberately not one. The
direction is the one D-2026-08-01 already chose where the rule is uncertain: **keeping a species
whole costs a cache miss, collapsing it wrongly puts a false record in the graph behind a human
signature; the first is recoverable and the second is not.** No shipped corpus contains such a
salt, so the cost today is zero and the risk it removes is live.

## Alternatives rejected

- **Consult the solvent table (`science/calc/solvents.py`).** Rejected twice over, and the second
  reason is the one that matters. First, `core` may import no sibling package — `tests/test_layering.py`
  enforces it both statically and by importing every `core` module in a clean interpreter — and
  `core/chem.py` is on the ELN ingest path, so the import is forbidden rather than merely untidy.
  Second, and fatally: `ALPB_SOLVENTS` is a set of **names** tblite's ALPB model accepts (`thf`,
  `dmso`, `nhexane`), with no structures in it at all. It cannot classify a *fragment*. Using it
  would mean writing a new solvent→SMILES table in `core`, which is a hand-maintained inventory of
  exactly the kind D-2026-08-01 rejected when it chose block ranges over an element list — and one
  whose misses fail *silently*, since a solvate of an unlisted solvent would go on collapsing.
- **Decoupling `Uncharger` from the strip entirely** (run it on everything the metal gate does not
  catch). **Built, measured, reverted.** It looked strictly better — it merges the ion-pair and
  neutral spellings of nicotine bitartrate onto one id, and differed from the shipped gate on only
  2 of 80 corpus structures. Then `tests/test_rxnfp.py` moved a pinned DRFP similarity from 0.7937
  to 0.7969, and the cause was the real one: `rxnfp._standardize_species` standardizes a reaction
  one `.`-separated token at a time, so `standardize` meets `[OH-]` and `[BH4-]` **alone**, without
  the counterion that explains their charge — and `Uncharger` turned them into water and **borane**.
  That is D-2026-08-01's NaOH and NaBH4 defect reappearing an ion at a time, on the live fingerprint
  path rather than in the reagent table where that ADR's tests watch for it. The shipped rule
  therefore neutralizes only species with at least one organic fragment, and
  `test_a_bare_inorganic_anion_is_not_neutralized_into_another_reagent` is the guard.
- **Leaving `Uncharger` coupled to the strip** (the pre-existing shape). Rejected because the
  solvate rule newly puts organic ion pairs on the kept-whole path, where the coupling would give
  one `compound_id` per spelling — trading this defect for the fragmentation D-2026-07-31 exists to
  prevent. Measured: the ion-pair and neutral spellings of nicotine bitartrate now agree.
- **A charge-based salt/solvate test** (an ion pair is a salt, a neutral pair is a solvate).
  Measured and rejected: nicotine bitartrate is written both ways in practice, so the test would
  make identity depend on the chemist's spelling — the exact property this whole pipeline exists to
  remove.
