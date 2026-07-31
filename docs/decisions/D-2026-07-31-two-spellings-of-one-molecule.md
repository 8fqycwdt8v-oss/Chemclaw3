# D-2026-07-31-two-spellings-of-one-molecule — Two spellings of one molecule, and two questions about them

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-011 (compute once), D-017 (one
fingerprint store), D-033 (one canonical identity scheme)

## Context

`core/chem.py` opens by saying the canonicalization that decides "same molecule" exists in exactly
one place. It did — and it was the wrong function. `Chem.MolToSmiles(Chem.MolFromSmiles(s))`
normalizes *spelling*: atom ordering, aromaticity perception, ring closures. Nothing else.

So a free base and its hydrochloride, a carboxylate and its sodium salt, and two tautomers of one
compound each produced a different string, and therefore a different `compound_id`, a different
calculation-cache key and a separate fingerprint row. There is no `rdMolStandardize` anywhere in
the tree.

This is the classic cheminformatics production failure, and its three consequences compound:
identity fragments in the graph, the cache misses work D-011 promises never to repeat (potentially
an HPC run), and similarity search ranks a molecule against itself as merely *similar* — which a
chemist reading the result cannot distinguish from two genuinely different molecules.

It is also the change that gets much more expensive after real data lands: `compound_id` keys the
graph, the calculation cache and both fingerprint indices, so retrofitting means migrating all four
at once. No committed note cites a `compound-<hash>` id today, which is why now.

## Decision

**Standardize — and split the one function into two, because there are two questions.**

The pipeline is deliberately the conventional one in the conventional order, because a bespoke
normalization is a bespoke notion of sameness: `Cleanup` → `FragmentParent` (strips counterions and
solvates) → `Uncharger` → `TautomerEnumerator.Canonicalize`.

Applying it everywhere is wrong, and the test suite said so immediately: `Uncharger` neutralizes
species a chemist meant as ions, and `Structure` validates a declared charge against its SMILES. A
calculation submitted for acetate must not silently compute acetic acid.

So:

- **`canonical_smiles` / `require_canonical_smiles` answer "is this the same *structure*?"** —
  spelling only, unchanged. They key the calculation cache, the QM workflow-dedup id and the
  prediction ledger, where an anion is a different calculation from its conjugate acid.
- **`standard_smiles` / `require_standard_smiles` answer "is this the same *compound*?"** — the
  full pipeline. They key `compound_id`, the fingerprint index, product↔reactant matching in
  `memory.chains` and species grouping in `memory.progression`.

Two names rather than one function with a flag, so a caller cannot pick the wrong one by leaving an
argument out, and each name says which question it answers. An earlier plan split them on
strict-versus-lenient so a note could "still display what the chemist wrote"; that was based on a
wrong premise — nothing displays either one, note bodies render the raw component SMILES, and every
caller uses these as keys.

**Stale rows are retired, not migrated.** `STANDARDIZATION_VERSION` is folded into the fingerprint
`definition` strings, and the store already refuses to rank across definitions — the guard built for
a changed ECFP radius, extended to the other thing that decides what a row *is*. A row indexed
before standardization is invisible to search until a re-index rebuilds it, which is failure-safe:
the alternative is answering a similarity question using two different notions of what a molecule
is. Calculation-cache rows need no bump at all: the key is derived from the structure, so an old row
is simply never looked up again.

**Reaction fingerprints move to the three-part form in the same change.** `reaction_smiles` put
solvent and catalyst on the reactant side, and DRFP hashes circular substructures over the whole
string — so a solvent, often the largest fragment and present in every run, contributed a large and
nearly constant share of the set bits. Similarity was dominated by the variable process development
is usually *optimizing*: two runs of one coupling in THF and in 2-MeTHF looked less alike than two
unrelated reactions sharing a solvent. Reagents stay on the left, because a base or an oxidant
participates stoichiometrically and is part of what the transformation is. `drfp:b2048:agents`
carries the change into the definition for the same reason as above.

## Consequences

One compound, one id, one cache entry, one fingerprint row. A chemist reporting a measurement on a
salt reaches the record for the substance.

**The cost is a re-index**, and it is deliberate rather than incidental: both fingerprint tables
must be rebuilt before similarity search returns anything for previously-indexed molecules. Search
returns *fewer* results in the meantime, never wrong ones, which is the failure mode worth choosing.

**Standardization is cached** (`lru_cache`, 4096 entries): tautomer canonicalization enumerates a
transform set, and the callers are loops over every component of every ingested reaction.

**What this deliberately does not decide.** The nine seed compound notes are still slug-named
(`compound-thf`) while the machine path mints `compound-<hash>` — a separate open row, and one
standardization does not touch. And stereochemistry is left exactly as RDKit reports it: collapsing
a racemate onto a single enantiomer is a chemistry decision with real consequences for a chiral
route, and it is not one to make as a side effect of stripping counterions.
