# D-2026-08-01-the-agent-slot-that-changed-no-bits — The agent slot that changed no bits

**Status:** accepted. Supersedes the "Reaction fingerprints move to the three-part form" section of
[D-2026-07-31-two-spellings-of-one-molecule](D-2026-07-31-two-spellings-of-one-molecule.md); the
rest of that ADR — molecule standardization, `STANDARDIZATION_VERSION`, the two questions
`canonical_smiles` and `standard_smiles` answer — stands unchanged.

## Context

D-2026-07-31 moved solvent and catalyst out of the reactant side of the reaction SMILES and into
the agent slot: `A.B.solvent>>C` became `A.B>solvent>C`. The reasoning was right and is repeated
below. The change did not implement it.

`DrfpEncoder.internal_encode` (drfp 0.3.7, the pinned version) begins:

```python
sides = in_smiles.split(">")
if len(sides[1]) > 0:
    sides[0] += "." + sides[1]
```

The agent slot is concatenated onto the reactants before anything is shingled, so the two forms are
byte-identical inputs to the encoder. Measured on a Suzuki coupling of 4-bromotoluene and
phenylboronic acid with K2CO3 and Pd(OAc)2, varying only the solvent, the THF and 2-MeTHF runs
score **0.8194** against each other under *both* forms. The reported fix moved the notation and
left the fingerprint exactly where it was.

Four places recorded that it had worked: the `reaction_smiles()` docstring, `reaction_definition()`,
the ADR section this one supersedes, and a `BACKLOG.md` row marked closed. The definition token was
even bumped to `drfp:b2048:agents`, retiring every previously-indexed reaction row in exchange for
bits that had not changed. A second thing was claimed and not delivered in the same place:
`STANDARDIZATION_VERSION` was folded into that token while `reaction_smiles()` built its string
from raw `c.smiles`, so the standardization half was bits-neutral for reaction rows too.

## Decision

**The agent-slot species are excluded from the fingerprinted string, not moved within it.**
Exclusion is the only operation DRFP can see. DRFP shingles each side and keeps the symmetric
difference; a species present only on the left — which every solvent and every catalyst is —
survives that difference whole, so it spends a large and nearly constant block of set bits on the
variable process development is usually *optimizing*. Excluded, the THF and 2-MeTHF runs of one
coupling score **1.0**: they are the same transformation, which is what campaign grouping
(`memory.optimization`) and `similar_reactions` are asking about. Conditions are recorded beside
the note; they are not part of the structure.

**Reagents stay on the left.** A base or an oxidant participates stoichiometrically and is part of
what the transformation *is*. Dropping the base would erase exactly the screens this system exists
to remember.

**The record form survives as a separate method.** `OrdReaction.reaction_smiles()` keeps returning
the three-part `reactants>agents>products` with the chemist's own spellings, and
`OrdReaction.transformation_smiles()` is the new fingerprint form. This is the part worth arguing
for: the three-part notation was never wrong, it was only mistaken for an encoding change. A note
body, a campaign step list and a playbook's representative reaction all render `reaction_smiles()`,
and a reaction note that no longer named its solvent would be a real loss in the graph's largest
note class. Two methods, each answering one question, rather than one string pressed into serving
a reader and a hash function whose needs are opposite.

`transformation_smiles()` puts every species through `standard_smiles`, so the
`STANDARDIZATION_VERSION` already in the definition token finally describes the rows it labels.

**The definition token moves to `drfp:b2048:agents-excluded:std4`.** Rows carrying either earlier
token encoded the solvent as part of the transformation, whichever slot it was written in, so the
store's existing refusal to rank across definitions retires them. The re-index cost is paid a
second time, which is the honest price of having paid it once for nothing.

## Consequences

Two runs of one coupling in different solvents are now the same fingerprint. That is the intended
behaviour and it is worth stating plainly: a caller who wants to distinguish them must read the
conditions, which are on the note and in the record, and not the bits. A structural index that
ranked by solvent was answering "was this run in the same flask" while being asked "is this the
same chemistry".

**Every previously-indexed reaction row falls out of similarity search until a re-index rebuilds
it.** Search returns fewer results in the meantime, never wrong ones.

**The claim is now measured rather than described.** `tests/test_rxnfp.py` asserts the numbers
above against the pinned encoder, including the fold-back that made the first attempt a no-op —
because the failure mode here is not a wrong fingerprint but a fingerprint that did not change
while four documents said it had. A test that compares the new form against the *raw* record form
is not enough: standardization alone moves the bits, so such a test passes with the exclusion
removed. It is held against the standardized three-part form instead, leaving the exclusion as the
only difference.

**What this does not decide.** A query string handed to `similar_reactions` is fingerprinted as
written — the tool has no roles to read, so it cannot exclude an agent the caller included, and it
is not standardized either. The tool documents `reactants>>products`, which now matches how the
index is built; making the query path symmetric is a separate change, and `BACKLOG.md` carries the
row this opens.
