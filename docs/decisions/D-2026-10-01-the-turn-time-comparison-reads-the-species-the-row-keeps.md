# D-2026-10-01-the-turn-time-comparison-reads-the-species-the-row-keeps — a species projection on `reaction_records`

**Status:** accepted · **Date:** 2026-10-01 · Closes the `BACKLOG.md` row *"The turn-time comparison
cannot diff what the ELN gives structured"* (issue #490) and the `DEFERRED.md` row *"Reagent and
solvent sets in 'changed vs previous'"*, whose trigger — the structured record becomes reachable at
turn time — this is.

## Context

`memory.progression.changes_between` diffs each role's canonical species set off `OrdReaction`, so
the *mined* `optimization-campaign` note names a run's swaps (`solvent DMF → 2-MeTHF`). The
**turn-time** comparison, `agent.condense._changes` behind `condense_protocols`, could not: a stored
run is a `reaction_records` row — `body`, `conditions`, the four filter fields — and the component
list survives only as prose inside `body`. It diffed temperature, time and a solvent a model read
back out of the procedure.

Two shapes were on the row. **(a)** A column on `reaction_records` carrying the per-role canonical
species sets — a projection, not the charge list. **(b)** Handing `Protocol` the full component list,
which `agent.condense` deliberately does not have, because a share document has none and a
component list with amounts is a second transcription of the record.

## Measured

On the seeded corpus (`Chemclaw3_mock`'s `app.eln.seed.seed_all`, mapped through the shipped JSON
and ORD adapters): 4,282 records mapped (the 5,760-record Suzuki flow screen is refused by the ORD
adapter today for name-only species, which is #482's subject; one JSON entry is malformed).

| | |
|---|---:|
| a row as JSON (`ReactionRecord.model_dump`), mean | 1,235 B |
| the species projection as JSON, mean | **217 B** (17.6% of the row) |
| the projection over the whole corpus | 0.93 MB |
| adjacent pairs inside the 59 mined campaigns | 4,175 |
| … where a species moved | 4,150 |
| … where a setpoint (temperature or time) moved | **0** |
| … where a non-solvent role moved (no prose solvent reader can see it) | 4,138 |

By role: reactant 3,956, reagent 485, catalyst 366, solvent 12. On the curated subset alone (the
twelve hand-written campaigns) every change is a solvent swap, which the prose reader can at best
approximate as text.

So before this, the "Changed vs previous" column rendered "unchanged" or `—` over **99.4%** of the
real changes a campaign carries, and the cost of ending that is ~200 bytes a row.

## Decision

**(a): `reaction_records.species`** (`infra/sql/111_reaction_record_species.sql`), `JSONB`, holding
`ingest.eln.ord.RoleSpecies` — the canonical SMILES of each compared role (reactant, reagent,
solvent, catalyst), no amounts, no order.

- `RoleSpecies` is the one definition of which roles are compared: `memory.progression.DIFFED_ROLES`
  is derived from its fields, and `OrdReaction.species(role)` is the one canonicalisation both the
  miner and the projection use. The miner and the turn-time table therefore cannot disagree about
  which roles count, and both go through `progression.species_change`.
- `record_from_ord_reaction` stores the projection; `PostgresReactionRecordStore` writes and reads
  it; `condense_protocols` resolves a `reaction-<id>` citation with it on `Protocol.species`.
- `_changes` diffs the roles when **both** protocols carry a projection. `None` — a note, a share
  document, a row ingested before 111 — is skipped, never read as four empty roles; an empty role
  *on* a projection is the record saying the run used nothing there and is diffed
  (`tasks/lessons.md` rule 77). When the sets were compared, the prose solvent is not compared a
  second time: the structured one is exact.

**(b) is declined**: a component list on `Protocol` would carry amounts and order the comparison
must ignore (`changes_between` excludes them because one run recording a mass and its neighbour not
reads as a change), would be a second transcription of a record whose rendering already lives in
`body`, and buys nothing the projection does not.

Revisit when: the comparison is asked to diff something the projection drops — an amount, an
equivalent count or an addition order becomes a column `progression.changes_between` itself diffs
(a field added to `ConditionChange`'s producers in `memory/progression.py`) — at which point the
projection must grow or (b) reopens.

## Consequences

- Rows ingested before migration 111 carry `species = NULL` and compare as before until their next
  sync rewrites them (the upsert refreshes every field); nothing is backfilled in SQL, because the
  projection needs RDKit canonicalisation.
- `infra/sql/README.md`'s `reaction_records` row names 111. #482 (citation-only ORD tier, open
  at the time of writing) also alters this table, in a file numbered 110 that `main` has since
  taken (`110_shared_sessions.sql`), so it renumbers past this one; its name-only species carry no
  structure, and `OrdReaction.species` is where they would have to be left out of the projection.
