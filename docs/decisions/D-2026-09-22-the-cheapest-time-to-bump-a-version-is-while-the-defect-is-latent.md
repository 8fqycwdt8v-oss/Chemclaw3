# D-2026-09-22-the-cheapest-time-to-bump-a-version-is-while-the-defect-is-latent — `_is_organic` and `std10`

**Status:** accepted · **Date:** 2026-09-22 · Supersedes nothing; amends no merged ADR. Closes the
`BACKLOG.md` row *"A bare guanidinium salt never reaches the neutralisation branch, so it does not
collapse onto its free base"*.

## Context

`core/chem.standardize` reaches the counterion strip and the neutralisation only when some fragment
is `_is_organic`, and that predicate was a C–H/C–C test: a carbon bonded to hydrogen or to another
carbon. The argument for it is in its own docstring and is sound as far as it goes — "does it
contain a carbon" calls carbonate organic, and `FragmentParent` then keeps `[O-]C([O-])=O` as the
parent of K2CO3 and throws the potassium away, which is how K2CO3, Cs2CO3, Na2CO3 and NaHCO3 once
collapsed into one compound.

Guanidinium's carbon has three nitrogen neighbours and no hydrogen. The row found that a bare
guanidinium salt therefore returns before both branches and does not collapse onto its free base,
while metformin and acetamidine do — **because their substituents happen to put a C–C bond
somewhere else in the fragment**. An identity that turns on where the chemist drew a methyl.

**Re-measured, the class is larger than the row knew.** `_is_organic` also returns False for
**urea**, thiourea, cyanamide and melamine. For a neutral single-fragment species that is
invisible, because `standardize` returns it unchanged either way; it bites on a salt or an adduct.
It is still wrong on its own terms: the docstring claims to implement "the classical
organic/inorganic line", and urea has been the canonical organic compound since 1828.

## Decision

**A carbon bonded to hydrogen, to carbon, or to two nitrogens**, and `STANDARDIZATION_VERSION`
moves to `std10` with it.

- **Two nitrogens rather than one, and that is the line that keeps the cyanides apart.** One
  nitrogen is cyanide (`[C-]#N`), cyanate (`[N-]=C=O`) and thiocyanate (`[S-]C#N`), every one of
  which must stay inorganic so NaCN and KCN stay two reagents — the case the original test was
  written for. Two is the urea/guanidine/amidine family. Driven over twenty species: every
  defended inorganic case holds (cyanide, cyanate, thiocyanate, carbonate, bicarbonate, CO, CO2,
  CS2, phosgene, CF4, azide) and urea, thiourea, guanidine and melamine move.

- **Cyanamide and dicyanamide land on the organic side, and that is not a new rule.** Their alkali
  salts now collapse onto the free acid — which is exactly what this module already ships for
  NaOMe, NaOtBu, LiHMDS and LDA, argued at the top of the file: the counterion is not part of the
  identity. A rule that treated them differently would be the bespoke notion of sameness the module
  opens by refusing.

- **No exception table.** The alternative that was considered and rejected was a structural
  carve-out for the nitriles (a triple-bond exclusion), which would have kept cyanamide inorganic.
  It buys one borderline species — cyanamide is catalogued as an organic reagent — at the price of
  a second clause that has no independent argument, and the NaOMe precedent above already settles
  the salt case. Rejected for the reason the docstring gives: an exception list is a table someone
  has to keep in step with the reagents chemists write.

- **The bump is taken now precisely because the defect is latent.** `durable/retention.py` records
  what a bump costs — a *permanent* doubling of `molecule_fingerprints` and `reaction_fingerprints`,
  because `app_privileges.sql` grants those tables INSERT and UPDATE only and nothing reclaims the
  superseded generation — and the only recovery is the runbook's re-sync. The row called the defect
  latent and treated that as a reason to weigh the fix. It is the opposite: **the generation a bump
  retires only ever grows**, so a defect that no shipped corpus reaches is at its cheapest moment,
  and every deferral makes the same bump more expensive. That is the third of the three reasons
  `std8 -> std9` was argued on, and it applies here with more force, because there the generation
  was hours old and here it is empty.

## Consequences

**Exactly one row of the shipped behaviour table moves.** `_STANDARDIZATION_AT_THIS_VERSION` is
measured output rather than hand-written expectation, and re-running it after the change red on
`NC(=[NH2+])N.[Cl-]` alone — now `N=C(N)N` — with the counterion strip, both neutralisation arms,
the three hydride charge states, the atom-map clear, the solvate, the metal–carbon bond and the
inorganic base all unchanged. Seven rows pinning the one-nitrogen side (both cyanides, thiocyanate,
cyanate, carbonate) and two pinning the new side (urea·HCl, melamine·HCl) are added beside it, so
the boundary is asserted and not merely described.

**A claim that had been narrowed is widened back.**
`test_an_amine_salt_drawn_as_an_ion_pair_is_its_free_base` carried a paragraph naming guanidinium
as the case its class claim could not reach. Guanidine and acetamidine hydrochlorides are now rows
in that list, so the sentence that used to except them is an assertion instead.

**The bump's second cost is the one `durable/retention.py` does not name, and it is still open.**
A definition bump retires the fingerprint rows, and `compound_id` carries no version — so a
superseded-spelling `compound_note` keeps its own id in the knowledge graph with no cleanup path,
and `compound_dependencies` re-derives `compound_id(note.compound_smiles)` and returns `[]` when it
no longer matches the note's own wikilink. That is the `BACKLOG.md` row *"A `STANDARDIZATION_VERSION`
bump retires the fingerprint rows and re-keys nothing"*, which stays open because both its candidate
fixes are decisions rather than defect fixes. It does not change the argument above — in this
repository's corpus the generation being retired is empty, so there is no superseded note to strand
— but a deployment that has ingested a guanidinium, urea or melamine salt inherits exactly that row,
and the recovery is the runbook's re-sync. Naming it here rather than only in the row, because this
is the second bump in as many weeks and the cost accounting in `retention.py` covers only the disk.

**Revisit when:** a corpus arrives holding an alkali cyanamide or dicyanamide salt whose counterion
a chemist is deliberately varying — sodium against calcium cyanamide is the real pair — where the
collapse onto the free acid discards the variable under study. That is the same cost
`D-2026-08-01` named for the base screen over NaOMe/NaOEt/KOtBu, and the answer would be the same
pKa-shaped predicate, not a carve-out here. The file that would show it is
`_STANDARDIZATION_AT_THIS_VERSION`, which would need a row for the salt in question and would fail
the day the behaviour is changed without the version.
