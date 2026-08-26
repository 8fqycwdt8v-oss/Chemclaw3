# D-2026-08-26-a-transcription-is-keyed-by-its-source — a `reaction_records` row is identified by its ingest source and the entry id

`052_reaction_records.sql` keyed the transcription tier on the bare `reaction_id` and put the
rendered provenance in a `source` column beside it. `ingest_reaction`'s own docstring named the
problem it was meant to answer:

> "two ELNs may legitimately use one entry id, which the fingerprint tables, keyed on the bare id,
> cannot represent. `ReactionRecord` keys on the bare id and carries its own `source` column beside
> it"

A column beside the key does not represent that. It records **which one won**.

## What was happening

`ON CONFLICT (reaction_id) DO UPDATE` refreshes every field, `source` included. So with
`CHEMCLAW_DATA_SOURCES=eln-databricks,eln-json` and both sites carrying `EXP-1001`:

```
rows: 1
surviving row -> eln-b:EXP-1001 | site B: nitration, failed
```

Site A's transcription is gone — not shadowed, deleted — and every `reaction-EXP-1001` citation a
playbook or campaign carries now expands into site B's failed nitration. `kg-validate` passes: the
citation resolves, to the wrong record. `reaction_labels` has keyed on `(source, reaction_id)`
since `051` for exactly this reason, and `eln-json`'s `_provenance` was changed to name its source
*because* of this collision — which made it visible in the body and did nothing to prevent it.

## Decision

**The row identity is `(ingest_source, reaction_id)`**, where `ingest_source` is the *registry
source name* — the string in `CHEMCLAW_DATA_SOURCES` — and not the rendered provenance already in
`source`. The two are different things and only the first is stable: `source` is a per-entry
citation string a binding's template renders, so keying on it would turn an entry whose provenance
rendering changed into a second row rather than an amendment of the first, which is the opposite
failure.

`ingest_source` is a store argument (`record(records, source)`, `bodies(ids, source)`) rather than a
field on `ReactionRecord`, because that model is what `record_from_ord_reaction` renders out of one
entry and the registry name is not in the entry. The store is where the two meet — the same place
`label_index.record(record_phase(reaction, source))` puts it.

`bodies` is scoped to the source too. It answers "is this replay unchanged?", and comparing one
ELN's page against another ELN's rows of the same ids is wrong in both directions: a false
"unchanged" skips a real entry, a false "changed" re-ingests one forever.

## What a citation does when two sources answer

`reaction-<id>` carries no source (`kg.note.note_id_for_reaction` spells the bare id), so with two
sites' rows behind one id there is genuinely no right answer. `read` **refuses**, naming both
sources, instead of returning one — a coin flip that reads as a fact is the failure this whole
finding is about. The remedy is an operator's: narrow the enabled sources, or have one site export
a distinct entry id.

This is a narrowing of the harm rather than its elimination, and the boundary is stated in
`ingest_reaction` rather than implied: the **fingerprint** tables are still keyed on the bare id, so
two sites sharing one id still collapse to one structural row. Fixing that means changing the id
space a citation is spelled in, which is a larger decision than this one and is not taken here.

## Migration and rollback

`056_reaction_record_identity.sql` adds the column and rebuilds the primary key.

**Rows written before it carry `''`.** Nothing in the row says which source wrote it and a backfill
would have to guess — `eln-json`'s provenance happens to begin with its source name, while a
warehouse binding's `provenance:` template need not mention it at all. Those rows stay readable:
`records._one_of` treats a *stated* source as superseding an unstated one, so a legacy row and its
own replacement are not read as a collision. The first sync after the upgrade re-writes each entry
under its real source. Clearing the leftovers (`DELETE FROM reaction_records WHERE ingest_source =
''`) is a reviewed operator step after a full re-sync, not a migration — this schema does not delete
(`D-2026-08-04-the-schema-only-goes-forward`).

**The rollback is not "deploy the previous image."** `ADD PRIMARY KEY` replaces the constraint the
previous image's `ON CONFLICT (reaction_id)` names, so every ingest write fails against it with "no
unique or exclusion constraint matching the ON CONFLICT specification" — the same shape `041`
measured. What an operator does instead, in order:

1. Stop the ELN sync schedule (`ElnSyncWorkflow`); reads are unaffected either way.
2. `ALTER TABLE reaction_records DROP CONSTRAINT reaction_records_pkey;`
   `ALTER TABLE reaction_records ADD PRIMARY KEY (reaction_id);` — this fails if two sources have
   already written the same id, and that failure is the correct one: the previous image cannot hold
   those rows, and choosing which to discard is a decision, not a rollback step.
3. Delete the `056` row from `schema_migrations` so a later roll-forward re-applies it.

`ingest_source` itself may stay: the previous image never names it and it has a default.

## Consequence

`reaction_records` is now the second table keyed by the pair, and the two agree. The rule that
generalises out of it, and out of `041` before it: **an index whose rows come from more than one
source has that source in its key, or it has a column that records which source overwrote the
others.**
