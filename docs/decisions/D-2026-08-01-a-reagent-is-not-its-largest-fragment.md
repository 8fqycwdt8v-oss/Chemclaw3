# D-2026-08-01-a-reagent-is-not-its-largest-fragment — A reagent is not its largest fragment

**Status:** accepted · **Date:** 2026-08-01 · **Refines:**
D-2026-07-31-two-spellings-of-one-molecule (the standardization pipeline and its version-bump
migration) · **Implements:** the full-codebase review's `core/chem.py` finding

## Context

D-2026-07-31 introduced `standardize()` to collapse two spellings of one molecule onto one
`compound_id`, so a free base and its hydrochloride stop fragmenting the calculation cache and both
fingerprint indices. The pipeline was `Cleanup` → `FragmentParent` → `Uncharger` → tautomer
canonicalization, applied unconditionally.

`FragmentParent` keeps the parent fragment and discards the rest. For an amine hydrochloride that
is exactly right: the amine **is** the compound and the chloride is a counterion. The pipeline was
written against that case and applied to everything.

Run the shipped `core/reagents.py` table through it and 15 of 87 entries were destroyed:

| written | stored as |
|---|---|
| NaOH, KOH | `O` — **water**, and therefore identical to each other |
| K2CO3, Cs2CO3, Na2CO3, NaHCO3 | `O=C(O)O` — one compound |
| NaBH4 | `B` — **borane** |
| LiAlH4 | `[AlH3]` |
| CsF, NaH | `[Cs+]`, `[Na+]` — the reactive anion deleted outright |

The review found this through one symptom: `memory/progression.py` diffs `Role.REAGENT`, so a
NaOH → KOH base screen produced a campaign note reporting that **nothing changed**. Two more
followed from the same cause — `compound_note` rendering three bodies (sodium hydroxide, potassium
hydroxide, water) under one id, and the fingerprint index gaining a *water* row for an NaOH
component.

Generalizing past the reported symptom found three more classes, each worse than the last:

- **Metal complexes.** Pd(OAc)2 → `CC(=O)O`, acetic acid. `reizman_suzuki` is a shipped BO
  benchmark, so a catalyst screen over Pd sources had the identical defect as the base screen.
- **Organometallics.** n-BuLi → **butane**. Pyrophoric reagent and fuel gas sharing a compound id,
  a cached calculation, a fingerprint row and a hazard screen.
- **Reagent names.** Fixing the above moved seven entries off the canonical key `reagents.py` is
  indexed by, so their compound notes rendered with no `- name:` line at all.

## Decision

**Strip fragments only when doing so preserves the species' identity.** Three questions, each asked
of the stage that still holds the evidence:

1. **Is any fragment organic?** — a carbon bonded to hydrogen or to another carbon. Not "contains a
   carbon": carbonate and bicarbonate contain one, and the K2CO3 = Cs2CO3 = Na2CO3 collapse above is
   precisely that mistake. Cyanide is inorganic by this test, which is why NaCN and KCN stay
   distinct.
2. **Does the species contain a d- or f-block metal?** — asked of the **cleaned** molecule, because
   `Cleanup` is what disconnects the metal into the fragment that would then be discarded. Group 1
   and 2 fall outside the block by construction, which is what lets sodium benzoate keep collapsing
   onto benzoic acid with no special case.
3. **Is there a metal–carbon bond?** — asked of the **input** molecule, because `Cleanup`'s
   `MetalDisconnector` is what destroys that evidence. This is the organometallic cut, and it is
   deliberately the mirror image of (2): the two checks look at different stages for opposite
   reasons.

An ionic salt of a group 1/2 metal has no M–C bond and collapses as before. A Grignard, an
alkyllithium, a cuprate and an organozinc all have one and are kept whole.

`STANDARDIZATION_VERSION` moved `std1` → `std3` across the two changes. It rides in both fingerprint
`definition` strings, so rows indexed under the old collapse fall out of similarity search rather
than being ranked against new ones — the migration mechanism D-2026-07-31 already established,
reused rather than reinvented.

`reagents.py` gains a reverse index keyed on the **standardized** SMILES, built once at import
(0.13 s) beside the existing canonical one, and `display_name` consults it as a second tier after
the exact-structure hit. `compound_note` derives its body from `require_standard_smiles`, matching
what `ingest/eln/ingest.py` already did, so one note id can no longer carry two bodies.

## Consequences

- 18 of 87 shipped reagents change identity; every one is a salt, a metal complex or an
  organometallic, and no organic entry moves. Verified entry by entry, before and after.
- All 87 spellings carry a name again. `memory/progression.py`, which renders reagent names from
  standardized SMILES, was hitting the same miss and is fixed by the same index.
- `synonyms_for` is deleted. It rescanned the whole table per note to answer the question the new
  index answers by lookup, and it answered it with the wrong key.
- **A conservative direction is chosen where the rule is uncertain.** Keeping a salt whole costs a
  cache miss; collapsing it wrongly puts a false record in the graph behind a human signature. The
  first is recoverable and the second is not.
- **Still open, deliberately.** `standard_smiles("CCN.C1CCOC1")` → THF: a solvate collapses onto
  whichever fragment is larger, since both are organic. Different mechanism from the counterion
  rule and not addressed here.

## Alternatives rejected

- **RDKit's `SaltRemover`.** Strips only fragments on a defined salt list, which reads like the
  principled choice — but it removes Na and K, so NaOH and KOH would both become `[OH-]` and stay
  identical. It solves the counterion question and not the identity one.
- **An element allowlist instead of the block ranges.** RDKit exposes no block or metal predicate
  (`PeriodicTable` has none; `rdMolStandardize` offers only the *disconnector*, which does the
  disconnection rather than the classification), so the block is spelled as atomic-number ranges in
  a named constant with its reasoning attached. A hand-maintained element list would have been one
  more inventory to forget to update.
- **Excluding metals from `_is_organic` instead of a separate check.** Conflates two questions that
  need different molecules — one the cleaned form, one the input — and the merged version silently
  loses the organometallic case.
