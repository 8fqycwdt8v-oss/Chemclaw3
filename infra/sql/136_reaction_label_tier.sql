-- A label row for a citation-only record: the facet tools count it, and nothing is derived for it
-- (D-2026-10-23-a-facet-on-a-drawn-species-counts-the-citation-tier).
--
-- `tier` is the evidence tier of the entry the row records, as `reaction_records.tier` (111) says it:
-- `structured` is every row there was before this column, and `citation-only` is a record whose
-- source named a species without its structure. A citation-only row carries what the source drew and
-- stated — the drawn species, the conditions, the yield, the workup — and **no `record_smiles`**: a
-- reaction nobody ran has no reaction string, and the labeller's atom mapping and named-reaction
-- match would be inferences from a partial structure. So the column is nullable, the CHECK below ties
-- it to the tier in both directions, the labelling scan reads `structured` rows only, and no
-- fingerprint or molecule row is written for the tier (`reaction_fingerprints` and `corpus_molecules`
-- are untouched: structure and similarity search stay closed).
--
-- **`DEFAULT 'structured'` is a true statement about every existing row**: until now an entry in the
-- other tier wrote no label row at all, so every row here records a reaction with a reaction string.
-- It is also true of what the previous image writes during a rollout, which names no `tier` and
-- always supplies a `record_smiles`.
--
-- **The meaning of `withheld` narrows.** 135 withheld the label row of every entry that was withdrawn
-- or named a species without its structure, because the tier had no label row to serve. Now a
-- citation-only entry has its own row and is served; `withheld` keeps only what it must: a withdrawn
-- entry's row, and the structured row an entry left behind when it was amended into the other tier
-- (the app role cannot delete it, and the next ingest of the entry replaces it with the tier's own
-- row and clears the flag). The block below re-derives the flag by that rule and clears anything a
-- rolled-back image derived for the tier, so it is also the restore: it is idempotent, and runs once
-- per registered tenant with `app.tenant` set because a forced row-level-security policy (`131`) shows
-- a migration role without a tenant no rows at all.
--
-- **The rows are not backfilled here.** A label row is written from the canonical record at ingest,
-- and `reaction_records` keeps a rendered body rather than the species and conditions; so entries
-- stored before this change get theirs when the source is re-synced (a one-off `ElnSyncWorkflow` with
-- `since` at the epoch, `docs/guides/runbook.md` §(v)). Until then every facet answer says how many
-- citation-only records on file it does not count.
--
-- **Rolling back keeps working and leaves the tier unmaintained.** The pre-136 image never names the
-- column and reads `record_smiles` of every unwithheld row as text, so its labeller would send a
-- citation-only row's missing reaction string to the labelling server and stamp the answer; re-running
-- the block below clears that. See the 136 row of `docs/guides/runbook.md`.
ALTER TABLE reaction_labels ALTER COLUMN record_smiles DROP NOT NULL;

ALTER TABLE reaction_labels
    ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'structured'
        CHECK (tier IN ('structured', 'citation-only')
               AND (tier = 'structured') = (record_smiles IS NOT NULL));

COMMENT ON COLUMN reaction_labels.tier IS
    'structured: the entry has a reaction string and is labelled by the drain. citation-only: the '
    'source named a species without its structure; the row holds what was drawn and stated, no '
    'record_smiles and nothing derived, and is counted by the facet tools in a tier of its own.';

-- The facet read for the tier is its own statement over this partial index, so the structured read
-- keeps its `labeller_version` ordering index (`reaction_labels_staleness_idx`) and an OR between
-- the two does not defeat it.
CREATE INDEX IF NOT EXISTS reaction_labels_citation_only_idx
    ON reaction_labels (source, reaction_id) WHERE tier = 'citation-only' AND NOT withheld;

DO $$
DECLARE
    found_tenant text := coalesce(current_setting('app.tenant', true), '');
    each_tenant text;
BEGIN
    FOR each_tenant IN SELECT id FROM tenants ORDER BY id LOOP
        PERFORM set_config('app.tenant', each_tenant, true);
        UPDATE reaction_labels AS l
           SET withheld = (r.retracted_at IS NOT NULL
                           OR (r.tier <> 'structured' AND l.tier = 'structured'))
          FROM reaction_records AS r
         WHERE r.ingest_source = l.source
           AND r.reaction_id = l.reaction_id
           AND r.tenant_id = l.tenant_id
           AND l.withheld <> (r.retracted_at IS NOT NULL
                              OR (r.tier <> 'structured' AND l.tier = 'structured'));
        UPDATE reaction_labels
           SET mapped_smiles = NULL, named_reaction = NULL, reaction_class = NULL, rxno_id = NULL,
               confidence = NULL, method = NULL, labeller_version = NULL, labelled_at = NULL
         WHERE tier = 'citation-only'
           AND (mapped_smiles IS NOT NULL OR named_reaction IS NOT NULL OR reaction_class IS NOT NULL
                OR rxno_id IS NOT NULL OR confidence IS NOT NULL OR method IS NOT NULL
                OR labeller_version IS NOT NULL OR labelled_at IS NOT NULL);
    END LOOP;
    PERFORM set_config('app.tenant', found_tenant, true);
END
$$;
