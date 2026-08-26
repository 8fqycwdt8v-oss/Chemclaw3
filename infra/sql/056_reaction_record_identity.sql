-- A transcription is identified by its source *and* the entry id (D-2026-08-26-a-transcription-is-
-- keyed-by-its-source).
--
-- `052` keyed `reaction_records` on the bare `reaction_id` and put the provenance in a `source`
-- column beside it. `ingest_reaction`'s docstring already named the problem — "two ELNs may
-- legitimately use one entry id" — and that column was not the answer to it: the upsert refreshes
-- every field including `source`, so with two ingest sources enabled the later sync silently
-- overwrote the earlier site's transcription, and every `reaction-<id>` citation a campaign or
-- playbook carried then resolved to a different run at a different site. `kg-validate` still
-- passed: the citation resolves, to the wrong record. `reaction_labels` has keyed on the pair
-- since `051`, for exactly this reason.
--
-- **`ingest_source` is the registry source name** (`CHEMCLAW_DATA_SOURCES`), not the rendered
-- provenance in `source`. The two are different things and only the first is stable: `source` is a
-- per-entry citation string a binding's template renders, so keying on it would make an entry whose
-- provenance rendering changed a *second row* rather than an amendment of the first.
--
-- **Rows written before this migration carry `''`**, because nothing in the row says which source
-- wrote it and a backfill would have to guess (`eln-json`'s provenance happens to start with the
-- source name; a warehouse binding's template need not). They stay readable — `records._one_of`
-- treats a stated source as superseding an unstated one, so a legacy row and its own replacement
-- are not read as a collision — and the first sync after the upgrade re-writes each of them under
-- its real source. Clearing the leftovers afterwards is a reviewed operator step, not a migration:
-- this schema does not delete (D-2026-08-04-the-schema-only-goes-forward).
ALTER TABLE reaction_records ADD COLUMN IF NOT EXISTS ingest_source TEXT NOT NULL DEFAULT '';

-- `ADD PRIMARY KEY` builds a unique index under an ACCESS EXCLUSIVE lock, as `041`'s did. On a
-- corpus-sized `reaction_records` that is seconds to a minute, once, applied by `make db-migrate`.
-- It also ends the rollback: the previous image's `ON CONFLICT (reaction_id)` no longer matches a
-- constraint, so every ingest write fails against it. What an operator does instead is in the ADR.
ALTER TABLE reaction_records DROP CONSTRAINT IF EXISTS reaction_records_pkey;
ALTER TABLE reaction_records ADD PRIMARY KEY (ingest_source, reaction_id);

COMMENT ON COLUMN reaction_records.ingest_source IS
    'The registry source name that transcribed this entry — half of the row identity, because two '
    'ELNs may use one entry id and the site that synced last must not overwrite the other. Empty '
    'on rows written before migration 056. Any row with a stated source supersedes those.';
