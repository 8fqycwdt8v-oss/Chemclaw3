# D-2026-09-22-the-parent-is-the-fragment-this-module-calls-organic — `std11`

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing; amends no merged ADR. Closes two
`BACKLOG.md` rows — *"Ammonium formate standardizes to ammonia"*, which it also **corrects**, and
*"A neutral co-former is discarded as if it were a counterion"* — because both are the same branch
asking somebody else a question it had already answered.

## Context

`core/chem.standardize` opens by arguing which fragment of a `.`-separated string is the compound:
"a salt has **exactly one** organic fragment, while a solvate has two or more", with `_is_organic`
the predicate and `D-2026-08-01-a-reagent-is-not-its-largest-fragment` the decision behind it.

Having decided that, it then asked somebody else. The keep-one-fragment branch called
`rdMolStandardize.FragmentParent`, whose `LargestFragmentChooser` counts atoms **including
hydrogens** and defaults to `preferOrganic=False`. Driven: on `[NH4+].[O-]C=O` the chooser keeps
`[NH4+]` — five atoms against formate's four — `Uncharger` then neutralises it, and **ammonium
formate standardizes to ammonia**. A transfer-hydrogenation reagent and a base share one
`compound_id`, one cache entry, one fingerprint row and one hazard screen.

**The row that recorded this was wrong about why**, and the correction is the reason it is worth an
ADR rather than a commit. It said "neither fragment is organic by any version of `_is_organic` — so
`organic == 0` is not the branch; the pick happens inside RDKit". Measured, formate **is** organic:
its carbon carries a hydrogen. The branch is the ordinary salt branch, taken correctly, handing the
decision to a chooser that answers differently. Prose about a mechanism, written without driving it.

**And the behaviour table could not see it.** `_STANDARDIZATION_AT_THIS_VERSION` has rows for this
branch — the amine hydrobromide, the pyridinium chloride, the sodium acetate — and on every one of
them `FragmentParent` and `_is_organic` agree, so the pipeline could be changed here without a
single row moving. A table of cases where two answers coincide is no evidence about which answer is
being used.

## Decision

**When exactly one fragment is `_is_organic`, that fragment is the parent.** Asked directly,
not delegated.

- **This is not a new rule, it is the shipped one applied.** The module's own sentence already says
  a salt has exactly one organic fragment; `_THE_ORGANIC_LINE` tests where that line falls over 42
  species. Nothing about the notion of "organic" changes here — what changes is that the code now
  uses it where it had been using a heuristic with a different metric.

- **`preferOrganic=True` was the alternative and is rejected.** It fixes this case — measured, the
  chooser then returns formate — but it substitutes RDKit's notion of organic ("contains a carbon")
  for this module's, which is precisely the notion `_is_organic`'s docstring exists to refuse: it
  calls carbonate and cyanide organic, which is how K2CO3, Cs2CO3, Na2CO3 and NaHCO3 once collapsed
  into one compound. Taking a flag that makes today's case pass, at the cost of re-adopting a
  predicate this file argues against, would be a fix that reads correct and is not.

**And the same branch discarded the rest without asking what they were.** Having chosen the parent
it dropped every other fragment on the strength of the count alone, never asking whether a
discarded fragment carries a charge or is a solvent — so urea hydrogen peroxide, a bench oxidant,
became urea, and ethylamine·H2O2 became ethylamine. A spectator now earns discarding two ways, and
between them they need no list of this repository's own:

- **It carries a charge.** The string balances, so an inorganic ion beside one organic fragment is
  that fragment's counterion by construction — bromide, sodium, nitrate, tetrafluoroborate,
  hexafluorophosphate.
- **It is a solvent RDKit's curated list knows.** A solvate of one organic fragment is that
  fragment, and a solvent list is exactly the table `D-2026-08-01` refuses to keep in step by hand.

**Asking that list alone was the first spelling and it regressed TBTU**, which is why the charge
clause leads. `FragmentRemover` is a *pharmaceutical salt* list: driven, it knows neither
tetrafluoroborate nor hexafluorophosphate, so TBTU kept its BF4 and HATU would have kept its PF6.
Reading the charge covers both without naming either, and without a table to maintain.

- **`STANDARDIZATION_VERSION` moves to `std11`.** Measured over every parseable carbon-bearing
  SMILES the tree holds — 6,481 of them across `data/`, `knowledge/`, `src/`, `tests/`, `docs/`,
  `skills/` and `schema/` — **three** standard forms change: ammonium formate from `N` to `O=CO`,
  and UHP and ethylamine·H2O2 from the stripped organic fragment back to the whole string. None of
  the 68 shipped reagent structures moves. The bump is taken for the reason
  `D-2026-09-22-a-version-bump-costs-the-same-whenever-it-is-taken` gives: a bump retires every row
  under the old definition whenever it is taken, so its cost does not grow with delay while the
  population keyed wrong does.

## Consequences

**The answer is formic acid, not "ammonium formate keeps its counterion".** That second question is
the one `D-2026-08-01` already settled the other way for NaOMe, NaOtBu and LDA — the counterion is
not part of the identity, and a base screen over three of them reads as three collapses. This
change does not reopen it; it stops the pipeline from keeping the *inorganic* half, which no rule
in this module ever asked for.

**Three rows join the behaviour table and one test drives the disagreement.**
`test_the_parent_is_the_fragment_this_module_calls_organic` asserts what
`LargestFragmentChooser` returns for ammonium formate, so the measurement is re-run rather than
remembered, and asserts that the two answers still coincide on every salt the table pins — which is
what makes "exactly one thing moved" checkable rather than claimed.

**The `FragmentParent` call is gone from this branch, and with it a class of surprises.** Anything
that changed in upstream's chooser — its metric, its tie-breaking, its salt list — used to be able
to change this system's notion of compound identity without a line of this repository moving. The
one remaining delegation on this path is `Uncharger`, which answers a different question.

**Revisit when:** a *charged* spectator turns out to be part of an identity rather than its
counterion, or a solvate a chemist means to keep is stripped because the list calls its partner a
solvent. Both are the same residual from the other side: the charge clause and the list are now the
whole of "what may be discarded", and neither reads intent. The nearest real candidate is an ionic
liquid used as a reagent, where the anion is the point. The file that would show it is
`_STANDARDIZATION_AT_THIS_VERSION`, which would need a row whose expected value is argued rather
than measured — and the drive to re-run is the corpus sweep above, which is what said three.
