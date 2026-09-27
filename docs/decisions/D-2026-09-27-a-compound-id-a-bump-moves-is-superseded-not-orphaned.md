# D-2026-09-27-a-compound-id-a-bump-moves-is-superseded-not-orphaned — `std12`

**Status:** accepted · **Date:** 2026-09-27 · **Owner decision** (2026-09-26). Supersedes nothing;
amends no merged ADR. Closes two `BACKLOG.md` rows, in the order the owner set: *"A
`STANDARDIZATION_VERSION` bump retires the fingerprint rows and re-keys nothing, so the graph keeps
a note per superseded spelling forever"* first, then — in the same version bump — *"A salt written
neutral and the same salt written ionic get two `compound_id`s, for the counterions RDKit's
catalogue omits"*.

## Context

`compound_id` is a hash of the standardized structure and carries no version, deliberately: a
citation to a compound note has to keep resolving. So a bump that changes what a structure
standardizes to moves that compound to a new id, and until now nothing followed it. The fingerprint
half behaved as designed — rows under the old definition fall out of search — and was recovered
only by deleting a corpus cursor and re-syncing the whole ELN. The note half had no recovery at
all: the old `compound_note` stayed current beside the new one, two notes for one substance, and
`compound_dependencies` re-derived the id, found it no longer matched the note's own wikilink, and
returned `[]`, so a pre-bump note re-recorded under the new version silently lost its compound.

Meanwhile `D-2026-09-22-the-parent-is-the-fragment-this-module-calls-organic` (`std11`) had named
its own trade: a neutral spectator is discarded only if RDKit's catalogue knows it, and for seven
ionisable acids it does not, so `CCN.OCl(=O)(=O)=O` kept its perchloric acid while
`CC[NH3+].[O-]Cl(=O)(=O)=O` stripped the perchlorate. Fixing that is itself a bump — so it is the
first bump that has to *re-key*, and the owner put the re-key first.

## Decision

**A: a compound id a bump moves is superseded, not orphaned.** The note under the new id records
`supersedes` for every old id that standardizes onto it; each old note is retired by
`memory.supersede.retire_note` — `valid_to` closed, `superseded-by` the new id, the body naming why;
both land through `kg.record.record_note`, in its load-bearing order (the successor, then the
retirements that cite it). Nothing is rewritten or deleted. The procedure is
`make rekey-compounds [APPLY=1]` (`cli/rekey_compounds.py` over
`memory.compound_rekey.rekey_standardization`): **a preview by default**, per-kind counts computed
before any write and identical either way, idempotent (an old note is a candidate only while its
window is open), interrupt-safe (one `record_note` per new id). It re-fingerprints the shelved rows
of both indexes too (`science/fingerprints/rekey.py`), insert-only, from what each row already
stores — so the runbook's full re-sync is no longer the only way back.

Readers follow the link. `kg.graph.current_successor` walks supersede links from either end — the
successor's `supersedes` or the retired note's `superseded-by` — through any number of bumps to the
note current on a date. `expand_note` on a superseded **compound** id returns the current note, with
a system notice outside the framed body; a neighbour that is a retired compound is reported as its
successor carrying the edge the author typed against the old id, so a note's compound neighbour
survives the bump. Current-evidence retrieval already drops the retired note by `valid_to`, so it
serves one note per substance. `compound_dependencies` returns the current compound for a note that
links a stale structure-derived id, and logs it.

**B: `_IONISABLE_NEUTRAL_ACIDS` in `core/chem.standardize`, behind a basicity gate.** The seven
acids are perchloric, tetrafluoroboric, sulfamic, thiocyanic (both tautomers), carbonic,
hypophosphorous and boric acid. A neutral spectator on this list is discarded like its anion **only
when the single organic fragment beside it could have taken its proton** (`_can_take_the_proton`).
That means the fragment carries one of:

- an aliphatic amine that is not an amide, carbamate, urea, sulfonamide, aniline, N–N or N–O nitrogen;
- an amidine or guanidine;
- a basic aza-aromatic nitrogen (pyridine-type, imidazole N3, and not pyrrole-type NH);
- a net positive charge.

The same gate applies to all seven acids, and none is special-cased.

**Why the gate.** An independent chemistry review blocked the first version, and the owner decided
the gate. Without it, the acid was stripped beside *any* single organic fragment. So a boronic acid
beside boric acid, or a compound transcribed with its carbonate buffer, took the parent's id and was
indistinguishable from a salt. That is the false merge
`D-2026-08-27-a-solvate-is-not-its-solvent` prevents for two organic fragments, and these acids
have no organic fragment of their own to trip that carve-out. Every test paired the acids with
ethylamine, so nothing caught it.

Matching is done after `Cleanup`, because `Cleanup` rewrites perchloric acid into a
charge-separated form that no hand-written SMILES matches. `tests/test_compound_identity.py` checks:

- every acid × seven basic partners (including a quaternary ammonium, the charge arm) converges;
- every acid × ten non-basic partners (boronic acid, phenol, carboxylic acid, amide, carbamate,
  sulfonamide, aniline, nitroarene, pyrrole, ester) keeps its own id;
- every acid registered alone is untouched.

Salt spellings and three mixtures are pinned in `_STANDARDIZATION_AT_THIS_VERSION`.
`STANDARDIZATION_VERSION` moves to `std12`. It has not shipped, so the gate stays in `std12`.

## Options weighed

- **Fold the version into `compound_id`.** Rejected: every stored id changes at every future bump,
  and every citation to one breaks — the failure the version-free id exists to prevent.
- **Rewrite the old notes in place.** Rejected: `kg/record.py` has no rewrite verb and the knowledge
  layer is corrected, not edited; an old id's history is the evidence a reader checks.
- **Supersede link (chosen).** Uses the vocabulary (`supersedes`/`superseded-by`), the retirement and
  the write order that already exist; the only new reader is `current_successor`.
- **Follow supersede links for every note type in `expand_note`.** Declined: a retired *claim* is a
  different claim, and a by-id lookup of one should keep returning it with its successor as a
  neighbour (KM-7). A compound's id *is* its identity, so its retired note is the same substance
  under a stale id.
- **Strip the acid beside any single organic fragment.** The first version did this, and review
  blocked it: it merges a mixture into its parent (see B).
- **A pKa-shaped predicate for B.** Declined for the reason `core/chem.py` opens by refusing a bespoke
  notion of sameness; the set is measured, closed and seven long, and
  `tests/test_compound_identity.py::test_the_table_is_exactly_the_acids_the_catalogue_omits` reds the
  day RDKit's catalogue carries one of them, so the table cannot drift into a second answer.

## Consequences

- **Re-keying derives the new id from the old standard form**, which is exact for a bump that
  discards or neutralizes more than its predecessor — every bump so far — and not for one that needs
  what the old form dropped. Such a bump still needs the source re-synced; the re-key never guesses.
- **`valid_to` is inclusive**, so the command closes an old id as of *yesterday*: closed today, both
  notes would stay current until midnight.
- A note a person wrote is never retired in place (`git_writer` refuses that amendment); the successor
  still supersedes it by name, and an existing successor a person wrote or was written for is not
  recorded onto (`blocked_successor`).
- The shelved fingerprint generation is still not reclaimed — `durable/retention.py`'s doubling
  stands; re-keying adds the current generation, it does not delete the old one.

## Measured

On the seeded corpus — `Chemclaw3_mock`'s ELN seed, 10,011 ORD entries of which the ORD adapter maps
4,251 (the 5,760 flow-Suzuki rows are refused, as a live sync refuses them) — with `std11` rows built
by running this build with `_IONISABLE_NEUTRAL_SPECTATORS` emptied, which *is* `std11`, into the
in-memory reference stores, on a host at load average 100–150:

| kind | rows | preview | apply | re-keyed | second apply |
| --- | --- | --- | --- | --- | --- |
| molecule fingerprints | 129 | 0.7 s | 0.6 s | 129, of which 3 moved key | 0 re-keyed |
| reaction fingerprints | 4,251 | 100.6 s | 103.7 s | 4,251, none moved key | 0 re-keyed |
| compound notes (one per molecule) | 129 | 1.35 s | — | 0 superseded: all 129 unchanged | — |

Preview and apply reported identical counts in both indexes. The seed holds no salt of the seven
acids, so **no identity moved** and the note half had nothing to write; the reaction time is DRFP
recomputed per row. `knowledge/` in this repository has 9 compound notes, all slug-named seed
notes, so none is a candidate (0.40 s).

**The three molecule keys that moved were not moved by the bump**, and that is the finding worth
keeping: they are ferrocenyl palladacycle precatalysts whose standard form is not a fixed point of
`standardize` (the Cp ring comes back kekulé from the raw string and aromatic from its own
standard form). Their citation does not move — `compound_id` hashes the standardized form, and both
spellings hash to one id — but it does mean `compound_id(raw)` differs from `compound_note(raw).id`
for them, which predates this change and carries its own `BACKLOG.md` row.
`tests/test_compound_rekey.py` builds the corpus that drives every branch of the re-key.

**Revisit when:** a bump changes a standard form in a way that needs information the old one
discarded (the re-key would then under-report; `plan_compound_rekey` is the file), or a non-compound
note type gains an id derived from content, which would make its retirement an identity change too.
