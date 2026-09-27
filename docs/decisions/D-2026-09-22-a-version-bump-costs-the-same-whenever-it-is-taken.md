# D-2026-09-22-a-version-bump-costs-the-same-whenever-it-is-taken — `_is_organic` and `std10`

**Status:** accepted · **Date:** 2026-09-22 · **Partially supersedes**
`D-2026-08-01-a-reagent-is-not-its-largest-fragment` — its decision item 1 ("Is any fragment
organic? — a carbon bonded to hydrogen or to another carbon") is the clause changed here; items 2-4
stand. Closes the `BACKLOG.md` row *"A bare guanidinium salt never reaches the neutralisation
branch, so it does not collapse onto its free base"*.

## Context

`core/chem.standardize` reaches the counterion strip and the neutralisation only when some fragment
is `_is_organic`, and that predicate was the C–H/C–C test above. The argument for it is sound as far
as it goes — "does it contain a carbon" calls carbonate organic, and `FragmentParent` then keeps
`[O-]C([O-])=O` as the parent of K2CO3 and throws the potassium away, which is how K2CO3, Cs2CO3,
Na2CO3 and NaHCO3 once collapsed into one compound.

Guanidinium's carbon has three nitrogen neighbours and no hydrogen. The row found that a bare
guanidinium salt therefore returns before both branches and does not collapse onto its free base,
while metformin and acetamidine do — **because their substituents happen to put a C–C bond
somewhere else in the fragment**. An identity that turns on where the chemist drew a methyl.

**The class is wider than the row, and wider than this ADR's first draft said.** The first draft
named urea, thiourea, cyanamide and melamine and claimed a drive "over twenty species" that nothing
in the tree reproduced. A review drove 125 and found 29 species change class: the cyanurates
(including trichloroisocyanuric acid and sodium dichloroisocyanurate), biuret, semicarbazide,
selenourea, nitroguanidine, dicyandiamide and 5-aminotetrazole among them. Every one is chemically
organic, so every new answer is right — but the understatement was the same failure the row itself
was written about, one level out. `_THE_ORGANIC_LINE` in `tests/test_compound_identity.py` is now
the drive, one row per species, so the next person who touches the predicate learns what moves by
running it (`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).

## Decision

**A carbon bonded to hydrogen, to carbon, or — at three or more heavy neighbours — to two of them
being nitrogen**, and `STANDARDIZATION_VERSION` moves to `std10` with it.

- **The coordination is read as well as the nitrogens, and that is the clause that took a
  measurement to get right.** The first spelling counted nitrogens alone. Measured, it put the
  cyanamide family on the organic side, and the family then split: `[Ca+2].[N-]=C=[N-]` reached
  `Uncharger` and came back as the **carbodiimide** tautomer while `[Na+].[NH-]C#N` came back as
  the nitrile one, which `_TAUTOMERS.Canonicalize` does not merge — so calcium cyanamide took free
  HN=C=NH's `compound_id` and a different one from sodium cyanamide. Two spellings of one
  substance under two ids is the failure this module opens by refusing, newly created by the fix
  for something else. Reading the degree costs one comparison and never reaches that molecule:
  three heavy neighbours with two nitrogens is the urea/guanidine/amidine family, two heavy
  neighbours is the linear family — cyanide, cyanate, thiocyanate, cyanamide, dicyanamide — which
  stays inorganic so its alkali salts stay distinct.

- **No exception table.** The rule is still structural and still has no element list behind it.
  The degree clause is not a carve-out for the cyanamides; it is the property that distinguishes
  substituted carbon from linear carbon, and the cyanamides fall out of it.

- **The bump is taken now, and the first draft's reason for that was wrong.** It argued that a
  latent defect is the cheapest moment because "the generation a bump retires only ever grows".
  That is false on this tree's mechanism: `STANDARDIZATION_VERSION` is a token in
  `molecule_definition()` and `reaction_definition()`, so a bump retires **every** row under the
  old definition — not the rows whose standard form changed — and `durable/retention.py` prices
  that as a permanent doubling because the runtime role holds no `DELETE`. The bill is the same
  whenever it is taken. What latency bounds is the damage of *not* taking it. The two together are
  still an argument for now rather than later, and a weaker one honestly stated: the cost does not
  grow, and the population of rows keyed under the wrong identity does.

  The same draft also said the retired generation "is empty". In this repository's corpus it is —
  zero of 328 parseable SMILES across `data/`, `knowledge/`, `src/`, `tests/`, `schema/`, `docs/`
  and `skills/` change standard form, and the only three that do are rows this change itself added
  to the test file. About a *deployment* it is a claim about production state, which the `std9`
  comment fifteen lines above forbids in as many words, and `std9` merged two days before this, not
  hours.

## Consequences

**Exactly one pre-existing row of the shipped behaviour table moves.** Re-driving all thirteen rows
of `_STANDARDIZATION_AT_THIS_VERSION` against the previous `chem.py` reds on `NC(=[NH2+])N.[Cl-]`
alone — now `N=C(N)N` — with the counterion strip, both neutralisation arms, the three hydride
charge states, the atom-map clear, the solvate, the metal–carbon bond and the inorganic base
unchanged. **Seven** rows are added: five pinning the two-coordinate side (both cyanides,
thiocyanate, cyanate, carbonate), two pinning the new side (urea·HCl, melamine·HCl), plus the two
cyanamide spellings and the cost below.

**The shipped reagent table and the metal-complex rules are untouched.** All 68 distinct structures
keep their standard form and their ids, with no new collisions. HATU and TBTU carry uronium carbons
with two nitrogens and were already organic through their N-methyls, so nothing moved. `standardize`
asks `_metal_is_the_compound` before it counts organic fragments, so ferrocyanide, ferricyanide and
every metal cyanide are unreachable from this change.

**One wrong identity is created and it is not `_is_organic`'s.** Urea hydrogen peroxide, a bench
oxidant, now takes urea's `compound_id`: `standardize` sends a string with exactly one organic
fragment to `FragmentParent`, which discards the rest "as counterions" and never asks whether a
discarded fragment is *charged*. The shape predates this — ethylamine·H2O2 already collapsed at
`std9`, when urea was called inorganic and UHP was kept whole by accident — and the fix is a
decision about which neutral co-formers are part of an identity, which moves every solvate at once.
It is pinned in `_STANDARDIZATION_AT_THIS_VERSION` and in
`test_a_neutral_co_former_is_stripped_like_a_counterion`, and carried as a `BACKLOG.md` row rather
than fixed here.

**The knowledge-graph half of the bump's cost is the one `durable/retention.py` does not name.**
`compound_id` carries no version, so a superseded-spelling `compound_note` keeps its id with no
cleanup path, and `compound_dependencies` returns `[]` when the re-derived id no longer matches the
note's own wikilink. That is an open `BACKLOG.md` row whose two candidate fixes are both decisions.
A deployment that has ingested a guanidinium, urea or melamine salt inherits it, and the recovery is
the runbook's re-sync.

**Revisit when:** `_THE_ORGANIC_LINE` needs a row whose expected value is argued rather than
measured — a species where the degree clause and the chemistry disagree. The nearest candidate found
while driving it is free carbodiimide, which this calls inorganic for consistency with cyanamide and
which a chemist would call organic; it is harmless today because no corpus holds it and nothing
keys on it, and the file that would show otherwise is `_THE_ORGANIC_LINE` itself, which fails the
day `_is_organic` changes without it.
