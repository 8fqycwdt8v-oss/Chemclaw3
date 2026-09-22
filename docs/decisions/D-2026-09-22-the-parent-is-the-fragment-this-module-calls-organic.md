# D-2026-09-22-the-parent-is-the-fragment-this-module-calls-organic — `std11`

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing; amends no merged ADR. Closes the
`BACKLOG.md` row *"Ammonium formate standardizes to ammonia"*, and corrects that row, which was
wrong about the mechanism.

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

- **`STANDARDIZATION_VERSION` moves to `std11`.** Measured over every parseable carbon-bearing
  SMILES the tree holds — 6,472 of them across `data/`, `knowledge/`, `src/`, `tests/`, `docs/`,
  `skills/` and `schema/` — **exactly one** standard form changes, and it is ammonium formate, from
  `N` to `O=CO`. The bump is taken for the reason
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

**Revisit when:** a string arrives with exactly one organic fragment where the organic half is
*not* the compound. The shape would be a co-crystal whose other component is a named reagent rather
than a counterion or a solvent — which is the open `BACKLOG.md` row about urea hydrogen peroxide,
approached from the other side: that row asks which *neutral* fragments may be discarded, and this
decision fixes which fragment is kept once the discarding is agreed. The file that would show it is
`_STANDARDIZATION_AT_THIS_VERSION`, which would need a row whose expected value is argued rather
than measured.
