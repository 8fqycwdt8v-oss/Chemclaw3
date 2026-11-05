# D-2026-10-23-a-salt-in-a-solvent-is-kept-whole — A salt in a solvent is kept whole

**Status:** accepted · **Date:** 2026-10-23 · **Owner decision** (2026-10-24, option 2) · **Refines:**
`D-2026-08-27-a-solvate-is-not-its-solvent` · **Closes:** the `BACKLOG.md` row *"A salt dissolved in
one organic solvent standardizes to the solvent"*

## Context

`core/chem.py::standardize` keeps the one organic fragment of a species and drops every charged
inorganic fragment beside it as "its counterion". That reading assumes the organic fragment carries
the charge the ions balance. When it is neutral, the ions balance each other and the organic
fragment is a solvent or a co-component, yet it still wins. Measured on `main` at `std13`:

| written | `standard_smiles` | `compound_id` equals |
|---|---|---|
| `[Li+].[AlH4-].C1CCOC1` (LiAlH4 in THF) | `C1CCOC1` | THF's |
| `[Na+].[H-].C1CCOC1` (NaH in THF) | `C1CCOC1` | THF's |
| `[Na+].[BH4-].CO` (NaBH4 in methanol) | `CO` | methanol's |
| `[Na+].[Cl-].CCO` (brine/ethanol) | `CCO` | ethanol's |
| `[Na+].[Cl-].c1ccccc1O` | `Oc1ccccc1` | phenol's |
| `CC(=O)O.[Na+].[Cl-]` | `CC(=O)O` | acetic acid's |

`compound_id` is the knowledge-graph note id, so a hydride reagent and its solvent reach one note,
one body and one fingerprint row — the failure D-2026-08-27 fixed for a solvate, reached through the
one-organic-fragment branch it left as "a salt". The genuine salts the branch exists for keep their
charge on the organic fragment (sodium benzoate, `[NH4+].[O-]C=O`, LDA, HATU, TBTU, a quaternary
ammonium bromide).

Blast radius, measured before proposing: the 68 shipped reagent structures
(`core/reagents.py::_RAW_SYNONYMS`), every `smiles` cell under `data/**/*.csv` and the
`Chemclaw3_mock` ELN seed, components included — 188 parseable structures, **0** change their
standard form under option 2. The D-011 calculation cache keys on `canonical_smiles` and is
unaffected.

## Options

1. **Keep the rule (`std13`)** — no cost; a reagent written with its solvent keeps taking the
   solvent's id, and the caller must split the string. The row stays a known wrong answer.
2. **A charged inorganic fragment is a counterion only when the organic fragment carries a charge**
   — beside a neutral organic fragment the ions balance each other, and the species is kept whole,
   as a solvate is (`C1CCOC1.[AlH4-].[Li+]`). One predicate in `standardize`; a version bump
   (`std14`), which retires every fingerprint row under `std13` and is recovered by
   `make rekey-compounds`. Buys: no salt-in-solvent string shares an id with its solvent or with
   the bare salt; consistent with D-2026-08-27 ("names no winner, keep whole").
3. **Drop a known solvent beside a balanced inorganic salt, keep the salt** — LiAlH4 in THF becomes
   LiAlH4. Chemically the reagent's identity, but asymmetric with D-2026-08-27 (an organic solute
   keeps its THF, an inorganic one would lose it); still needs option 2 for a neutral organic that
   is not on RDKit's solvent list (phenol, acetic acid); same version bump.
4. **Refuse the string** (`InvalidSmilesError`) when a neutral organic fragment sits beside a
   balanced salt — forces the split upstream, but makes an ELN row with such a cell bad data and
   drops it from ingest.

## Decision

Decided by the owner 2026-10-24: **option 2**. It is the smallest change that makes every measured case distinct from
both its solvent and its salt, it extends an accepted rule instead of introducing a second notion
of "solvent", and on the shipped data it moves nothing. Option 3 can be layered later as a
reagent-vocabulary concern if chemists want "LiAlH4 in THF" to resolve to LiAlH4; the identity
pipeline should not guess which component is the reagent. The owner accepted the `std14` bump.

## Consequences

`standardize` keeps a charged inorganic fragment beside a neutral organic one;
`STANDARDIZATION_VERSION` is `std14`. Pinned by
`tests/test_compound_identity.py::test_a_salt_in_a_solvent_is_not_the_solvent` and three rows in
the version table. After merge, each deployment holding fingerprint rows must run `make rekey-compounds`
(preview, then `APPLY=1`; `docs/guides/runbook.md`).

Revisit when: a chemist-facing eval shows reagent-in-solvent strings should resolve to the reagent
(option 3), or `tests/test_compound_identity.py`'s version table needs a row where a neutral organic
fragment must lose a balanced salt.
