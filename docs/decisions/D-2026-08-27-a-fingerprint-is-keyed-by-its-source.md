# D-2026-08-27-a-fingerprint-is-keyed-by-its-source — a `reaction_fingerprints` row is identified by its ingest source and the entry id

`D-2026-08-26-a-transcription-is-keyed-by-its-source` fixed this class for `reaction_records` and
said in writing what it was leaving undone:

> "the **fingerprint** tables are still keyed on the bare id, so two sites sharing one id still
> collapse to one structural row. Fixing that means changing the id space a citation is spelled in,
> which is a larger decision than this one and is not taken here."

`ingest_reaction`'s docstring named the same gap in the tree's own words — "this function writes
four indexes and only three of them can tell the two sites apart." This ADR is the fourth.

## What was happening, measured

Against the live database, on the code as it stood, ingesting `EXP-1001` from `eln-a` (an
esterification) and then the same entry id from `eln-b` (a bromination), through the production
`ingest_reaction` with the Postgres fingerprint and record stores:

```
reaction_fingerprints rows: 1
    ('EXP-1001', 'c1ccccc1.BrBr>>Brc1ccccc1')
reaction_records rows:      2      -- [('eln-a', 'EXP-1001'), ('eln-b', 'EXP-1001')]

search for site A (CCO.CC(=O)O>>CCOC(C)=O): []
  verdict: "No indexed reaction matched this query. The reaction fingerprint index holds records
            and was searched, so this is a genuine negative result."
search for site B (c1ccccc1.BrBr>>Brc1ccccc1): [('EXP-1001', 1.0)]

note id for A: reaction-EXP-1001
note id for B: reaction-EXP-1001
```

Two things in that output matter more than the row count.

**Site A's chemistry is not shadowed, it is gone.** `056` kept its *transcription* — the record row
survived, and a citation is refused rather than answered with site B's run. But the DRFP bits were
overwritten, so the esterification is not in the structural index at all. Search the index for the
exact reaction it was handed seconds earlier and it answers nothing.

**And it answers nothing under the wrong sentence.** `FingerprintSearch.verdict` exists to keep
"nothing matched" and "nothing was indexed" apart, because a live run once told a chemist we had no
precedent for a structure while 1,025 notes sat un-backfilled. Here the index *is* populated, so the
probe is correct and the verdict is "this is a genuine negative result" — the exact failure that
model was written to prevent, arriving through the primary key instead of through an empty table.
A chemist is told the company has never made this, about an experiment their own site ran.

## Decision

**The row identity is `(source, id)`**, where `source` is the registry source name — the token in
`CHEMCLAW_DATA_SOURCES` — and `id` is the entry id it always was. That is the same pair, meaning the
same string, as `reaction_labels.source` (`051`) and `reaction_records.ingest_source` (`056`), so the
four indexes one ingest writes now agree on what a source is. `ingest_reaction` passes the source it
is already given; nothing derives it.

The column is named `source` rather than `ingest_source` because this table has no other claimant on
the word. `reaction_records` needed the longer name only because its `source` column already held a
rendered provenance string.

**The molecule index is deliberately not changed, and that is not an unfinished half.** A
`molecule_fingerprints` row's id is its standardized SMILES, so two sites charging the same reagent
are one structure and *must* share one row; splitting that index by source would duplicate every
shared structure and answer "have we made this?" per site. What needs a source in its key is an
index whose ids come from outside this system. `FingerprintRecord.source` therefore defaults to
empty, and `PostgresFingerprintStore` takes a `source_keyed` flag rather than growing a second class
— everything that makes that backend worth having (the Jaccard SQL, the HNSW ordering, the
definition scoping, the pooling) is identical either way, and the divergence is confined to the key
columns its statements are built from. A record carrying a source handed to a store that is not
`source_keyed` is **refused**, because the column does not exist and the value would otherwise be
dropped on the floor — which is the silent half-write this whole key change is about.

## The rows already stored are backfilled exactly, not guessed

`056` had to default its column to `''` and wait for a re-sync, because nothing in a
`reaction_records` row said which source wrote it. Here something does, in the same database:
`ingest_reaction` writes the label index's record phase and the fingerprint row **from one call**,
so every fingerprint row ingested since `051` has a `reaction_labels` row carrying its true source.
`063` joins on it, restricted to ids exactly one source claims:

```sql
UPDATE reaction_fingerprints f SET source = claimant.source
FROM (SELECT reaction_id, min(source) AS source FROM reaction_labels
      GROUP BY reaction_id HAVING count(*) = 1) AS claimant
WHERE f.source = '' AND f.id = claimant.reaction_id;
```

An id two sources already claim is the collision itself: one surviving fingerprint row could belong
to either, and assigning it to the alphabetically-first source would be the coin flip this finding
is about. It is left under `''`.

**What stays `''` is bounded, named, and transient.** Rows ingested before `051` existed, and rows
whose id two sources claim. Leaving them alone was not an option the way it was for `056`: two rows
with identical bits and one label are two *hits*, so a similarity search would report two precedents
where a chemist has one experiment. So a sourced write deletes the row's unsourced twin
(`PostgresFingerprintStore.add`, one primary-key lookup, matching nothing once an index is clean).
That is the write path's statement of the rule `reaction_records._one_of` already states on the read
path: a stated source supersedes an unstated one.

A full reset was the alternative and is not needed. It is worth recording why it was considered
credible at all: **this index is derived and rebuildable**, unlike a transcription — nothing in it
is a fact the system could not regenerate. But "rebuildable" here means re-running the ELN sync from
a reset cursor, an operator action with a real cost, and `make reindex`/`reindex-full` rebuild the
*note* index and not this one. An exact backfill plus a self-healing write path costs an operator
nothing, so the destructive option was not taken.

## The note id: what changed, and what deliberately did not

`note_id_for_reaction(record_id, source="")` now spells a source-qualified citation when given a
source, and the bare `reaction-<id>` when not. **Every caller in `src/` passes no source, so no
citation this system writes today has changed shape**, and a single-source deployment — one source,
one row per entry id, nothing to disambiguate — keeps the bare form forever. `knowledge/` holds no
`[[reaction-…]]` citation to break (grep: zero, one prose mention in `knowledge/README.md`).

The separator is `.`, and the choice is constrained rather than aesthetic: `:` is out twice over —
`require_note_slug` refuses it, and `split_link` reads a colon as a relation, so
`[[reaction-eln-a:EXP-1001]]` would parse as an edge of type `reaction-eln-a`. `-` and `_` appear in
every second entry id. No source name in this tree contains a `.`, and one that did is **refused**
at citation time rather than producing an id that cannot be split back.

**The qualified form is defined and not yet resolved anywhere, and that is stated rather than
implied.** The readers that would have to accept it — `agent.graph_tools.expand_note`,
`agent.protocol_tools`, `ingest.eln.records.read`, `ingest.labels.record.record_phase`,
`retrieval.retrievers` and `connectors.rxnfp.tools` — still spell and strip the bare form, so passing
a source today produces an id nothing can look up. It is defined here anyway, and only here, for the
reason the function exists at all: the last time this spelling was left to its callers, three of them
invented it and one got it wrong, and a chemist handed a search hit was told the note did not exist.

**Adopting it is a knowledge-graph identity change and is the follow-up this ADR names.** A citation
would start carrying a source, `EXTERNAL_ID_PREFIXES`/`external_record_id` would need the inverse
split, and `ReactionRecordStore.read` would take the pair — at which point
`AmbiguousReactionRecord` stops being the answer for a hit the search could already identify. Until
then the harm is narrowed, not eliminated: both sites' reactions are *findable*, and a citation that
two sources could answer is refused rather than guessed (`056`), which is where a reader is left
today.

## Migration and rollback

`063_reaction_fingerprint_source.sql` adds the column, backfills, and rebuilds the primary key.

**The rollback is not "deploy the previous image."** `ADD PRIMARY KEY` replaces the constraint the
previous image's `ON CONFLICT (id)` names, so every fingerprint write fails against it. That is
measured, not predicted — running the existing suite against a migrated database with the old
constructor still in place produced exactly it:

```
psycopg.errors.InvalidColumnReference: there is no unique or exclusion constraint matching the
ON CONFLICT specification
```

the same shape `041` and `056` measured. What an operator does instead, in order:

1. Stop the ELN sync schedule (`ElnSyncWorkflow`); reads are unaffected either way.
2. `ALTER TABLE reaction_fingerprints DROP CONSTRAINT reaction_fingerprints_pkey;`
   `ALTER TABLE reaction_fingerprints ADD PRIMARY KEY (id);` — this fails if two sources have
   already indexed the same id, and that failure is the correct one: the previous image cannot hold
   those rows, and choosing which site's chemistry to discard is a decision, not a rollback step.
3. Delete the `063` row from `schema_migrations` so a later roll-forward re-applies it.

`source` itself may stay: the previous image never names it and it has a default.
`tests/test_migrations_are_additive.py` carries the reviewed exemption for the two statements, keyed
to this ADR.

## Consequence

`reaction_fingerprints` is the fourth index keyed by the pair, and `ingest_reaction`'s docstring is
true rather than a standing note of a gap. The rule `056` generalised holds unchanged and has now
been applied everywhere it applies in this ingest path: **an index whose rows come from more than one
source has that source in its key, or it has a column that records which source overwrote the
others.** The fingerprint table had neither — it had a key that made one site's chemistry disappear
and a verdict that called the disappearance a fact.
