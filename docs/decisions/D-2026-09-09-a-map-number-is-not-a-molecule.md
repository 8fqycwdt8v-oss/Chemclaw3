# D-2026-09-09-a-map-number-is-not-a-molecule — A map number is not a molecule, and a stereocentre the fingerprint cannot see is not preserved

**Status:** accepted · **Date:** 2026-09-09 · **Extends:**
D-2026-07-31-two-spellings-of-one-molecule (the two questions and the retirement lever),
D-2026-08-01-a-reagent-is-not-its-largest-fragment (the three guards on the strip),
D-2026-08-27-a-solvate-is-not-its-solvent (the organic-fragment count) ·
**Implements:** the wave-11 review's `core/chem` and fingerprint findings

## Context

Four measurements against `src/chemclaw/core/chem.py` and `science/fingerprints/`, all on the
success path, none visible to any existing test.

### 1. DRFP is not invariant to atom mapping

DRFP shingles atom environments **as SMILES strings**, and an atom-map number lives inside those
strings. `standard_smiles` did not strip them, so `_standardize_species` handed DRFP the maps
intact. Measured on one esterification written four ways:

| pair | Tanimoto |
|---|---|
| unmapped vs fully mapped | **0.0000** |
| unmapped vs half mapped | 0.4074 |
| mapped vs half mapped | 0.1277 |
| mapped vs the same reaction renumbered | **0.0000** |

The default threshold is 0.3, so the first and last return nothing. `CLAUDE.md` names Pistachio as
the first live integration and Pistachio reaction SMILES are atom-mapped, while
`ingest/eln/ord.py::transformation_smiles` builds ELN rows from component SMILES that are usually
unmapped — so `corpus_reactions` and `reaction_fingerprints` were **mutually unsearchable**, which
arrives as "we have no precedent" for a reaction on file in both. The numbering is RXNMapper's
choice, so the last row is not hypothetical: re-labelling one corpus renumbers every reaction in it.

`science/labels/reactions.py::transformation_of`'s docstring claimed `drfp_bitstring`
"standardizes every species itself before folding, which is what makes rows in the two tables
comparable at all". That was false in exactly this case, and this ADR is what makes it true.

The same string mints `compound_id`, so the defect had a second half: mapped and unmapped acetic
acid produced `compound-4715f6a1bca1` and `compound-9ac385b135af` — two notes, two index rows,
identical bits, tied at 1.0000. That is the identity fragmentation D-2026-07-31 exists to prevent,
for the one spelling that pipeline did not normalise.

### 2. ECFP4 discarded stereochemistry while `core/chem.py` said it did not

`core/chem.py` carries twelve lines arguing that `TautomerEnumerator`'s stereo-erasing defaults are
the wrong rule for an identity function, and states in the present tense that the preserved stereo
"is folded into `compound_id`, into the **ECFP4** and DRFP fingerprint rows". Measured: true of
`compound_id`, true of DRFP, **false of ECFP4** — `GetMorganGenerator` leaves `includeChirality` at
RDKit's `False` default, so (S)/(R)-naproxen, L/D-alanine, R/S-thalidomide and every E/Z pair
produced **byte-identical** rows.

The consequence is worse than a merged record, because the two halves disagree. `standardize` keeps
the enantiomers apart, so each holds its own `compound_id` and its own note, and
`MoleculeHit.for_molecule` derives the citation from the *stored* structure. Driven through
`find_matches` with (R)-naproxen as the query:

```
  1.0000  COc1ccc2cc([C@@H](C)C(=O)O)ccc2c1     <- the (S) enantiomer, ranked FIRST
  1.0000  COc1ccc2cc([C@H](C)C(=O)O)ccc2c1      <- the exact match, second
```

`ORDER BY similarity DESC, source COLLATE "C", id` breaks the tie on the label, so the wrong
enantiomer outranks the query's own structure — at a score that says *identical*, over a citation
to a different compound's note. This is D-2026-08-01's thalidomide argument reproduced one layer
down, inside the fix for it.

### 3. The NaOH class survives for every alkali salt of an *organic* conjugate acid

The three guards on the strip key on a d/f-block metal, an M–C bond and the organic-fragment count.
None fires for an alkoxide, an amide or an acyloxyborohydride — one organic fragment, a group-1
counterion, no M–C bond:

```
NaOtBu -> CC(C)(C)O   KOtBu -> CC(C)(C)O   LiOtBu -> CC(C)(C)O   tBuOH -> CC(C)(C)O
NaOMe  -> CO          NaOEt -> CCO         LiHMDS -> HMDS        LDA -> iPr2NH
NaBH(OAc)3 -> CC(=O)OB(OC(C)=O)OC(C)=O   ==   B(OAc)3      # the hydride is gone
```

The last row is not the same defect as the others, and separating them is the whole decision below.
Stage by stage, `Uncharger` took the hydrogen count from **10 to 9**: boron has no room for a fourth
substituent, so the only route to a neutral species is to remove the hydride. Sodium
triacetoxyborohydride standardized into triacetoxyborane, a Lewis acid that reduces nothing.

### 4. Found sound, and deliberately not touched

Bit collisions and saturation at 2048/r2 (vancomycin 133→130 environments; peak occupancy 6.3%);
`tanimoto` symmetry over 528 pairs; the all-zero, width and NaN-threshold guards; DRFP's invariance
to reactant order, product order, spelling and the agents-excluded form; one generator, one config,
one `definition` across all four producers.

## Decision

**Atom map numbers are cleared in `standardize`, and `STANDARDIZATION_VERSION` moves `std6` →
`std7`.** In `standardize` rather than at the DRFP boundary because the same function mints
`compound_id`, so one line closes findings 1 and 4 together. `canonical_smiles` is deliberately
left alone: it answers "is this the same *structure*?" for the calculation cache and the QM dedup
id, where the key is what the caller submitted, and nothing submits a mapped species to be
computed.

**ECFP4 is built with `includeChirality=True`, and the definition string says so** —
`ecfp:r2:b2048:chiral:std7`. Not a setting, unlike radius and width: those are genuine trades
between resolution and storage, while this one decides whether the index agrees with the identity
function feeding it, and a deployment that turned it off would be re-introducing the defect rather
than tuning anything. The token is in the definition for the reason `agents-excluded` is on the
reaction side — a flat row and a chiral row are the same width and are not comparable, and riding
on `STANDARDIZATION_VERSION` alone would retire them once and then let a later pipeline change move
that token while this one silently stayed true.

**A neutralization that removes a hydrogen is not a protonation, and does not run.**
`_neutralization_is_protonation` is the fourth guard: "the counterion meets its conjugate acid"
assumes the anion can *be* protonated, and where `Uncharger` instead reaches neutral by taking an
atom away, the species is kept with its charge — which is what `[BH4-].[Na+]` already gets one
branch up. The test is the hydrogen count rather than an element list or a pKa table, because the
property being asserted is what the *transformation* did, and a rule written over boron would miss
whatever the next such anion is made of.

**The alkoxide, amide and thiolate collapses are accepted, explicitly, and asserted.** KOtBu and
tert-butanol keep one `compound_id`; so do NaOMe/MeOH, LiHMDS/HMDS and LDA/diisopropylamine. That
is the same rule as sodium acetate and sodium benzoate, which D-2026-07-31 decided and this suite
has pinned since. Separating them needs a pKa-shaped predicate — "an anion whose conjugate acid is
weak enough that the salt is the reagent" — and a bespoke normalization is a bespoke notion of
sameness, which is what `core/chem.py` opens by refusing. It is written into a test rather than
left implicit so that whoever revisits it finds a decision rather than a gap.

## Consequences

**Both fingerprint indices must be re-indexed, and this is one re-index rather than three.** Every
`ecfp:*:std6` and `drfp:*:std6` row is invisible to search until it is rebuilt; the store already
refuses to rank across definitions, so search returns *fewer* results in the meantime and never
wrong ones. All three changes ride the same bump, so enabling chirality costs no re-addressing
beyond what the atom-map fix already forced.

**`STANDARDIZATION_VERSION` has a third consumer outside the fingerprints**, and the bump reaches
it: `ingest/labels/labeller.py` folds the version into the string that decides whether a stored
label row is stale, so **every labelled row is re-labelled on the next drain pass**. That is the
designed behaviour — the species SMILES sent for classification were normalised by rules that have
changed — but on a corpus of Pistachio's order it is a full re-label, and whoever owns the drain
should expect it rather than discover it.

**Similarity falls where a query does not specify stereo and the record does.** Measured over
stereo-dense molecules against the shipped 0.3 threshold: glucose 1.0000 → 0.4000, cholesterol →
0.4776, sucrose → **0.3061**. The neighbour is still returned in every case, so this is a cost in
*ranking* where the alternative was a citation error the reader cannot detect — but sucrose is close
enough to the floor that a still denser polyol could fall under it, and
`tests/test_stereo_identity.py` asserts the floor so that it cannot quietly get worse. Whether
`fingerprint_similarity_threshold` should drop is a separate measurement over a real corpus, not a
guess to fold in here.

**Achiral molecules pay nothing**: `CCO`, benzene, aspirin and DMF fingerprint byte-identically
with the flag on, and bit density is unchanged on the stereo-dense cases measured (dipeptide 25 →
25 bits, glucose 17 → 18).

**Isotopes stay invisible to ECFP4, and that is not a defect being deferred.** `CCO` and
`[13CH3]CO` tie at 1.0000 with the flag on as well, because ECFP4's atom invariant is connectivity
plus element, charge, degree and H count. An isotopologue *is* the same connectivity; `compound_id`
already tells the two apart, which is the half that keeps a labelled tracer from being filed as its
unlabelled parent.

## Alternatives rejected

- **Strip atom maps inside `_standardize_species` (the DRFP boundary).** Fixes the reaction half
  and leaves `compound_id` fragmented, so the corpus still writes two notes per mapped compound.
- **Correct the stereo sentence instead of the generator.** Coherent, and it is the cheaper change:
  a stereo-blind fingerprint is *more* tolerant, which is arguably what "something like this"
  wants, and it would keep the sucrose case at 1.0000. Rejected because the tolerance is not the
  problem — the contradiction is. The system had already decided stereo is identity, at the cost of
  configuring the tautomer enumerator against its own defaults and a suite pinning six pairs; a
  retrieval layer whose score says "identical" about a row whose citation says "a different
  compound" cannot be repaired by rewording either half.
- **Make `includeChirality` a setting.** Radius and width are settings because both answers are
  defensible. This one has a wrong answer, and a knob is an invitation to pick it.
- **A pKa or conjugate-acid-strength predicate for the alkoxides.** Would split NaOMe from methanol
  and would also split sodium benzoate from benzoic acid, which is the case D-2026-07-31 exists
  for. There is no in-process pKa either, since `D-2026-08-16-the-physics-leaves-the-cache-stays`
  moved it to `Chemclaw3-mcp`.
- **Widening `_is_organometallic` to a carbanion written as a separated ion pair.** Measured:
  `C[CH-]CC.[K+]` standardizes to butane, which is D-2026-08-01's "a pyrophoric reagent and an
  alkane sharing one compound id" spelled without the bond. Left unfixed on purpose — real corpora
  write n-BuLi as `CCCC[Li]`, no measurement here says the ion-pair spelling occurs, and the
  obvious guard (refuse to protonate a carbon) would also hold sodium acetylacetonate apart from
  acetylacetone, which nobody has asked for. Recorded so the next reviewer starts from the
  measurement rather than from the absence.
