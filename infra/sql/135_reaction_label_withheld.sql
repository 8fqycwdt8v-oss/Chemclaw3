-- A label row follows its entry: `withheld` is true while no facet question may serve the row,
-- because the ELN entry it records was withdrawn by its source or amended to name a species without
-- its structure (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable).
--
-- `reaction_labels` is INSERT/UPDATE-only for the app role (`grants/app_privileges.sql`), and a
-- citation-only entry writes no record phase, so an entry amended out of the structured tier kept the
-- label row of its structured past and the facet tools kept counting it; a withdrawn entry was
-- re-recorded and kept answering them too. `ingest_reaction` now writes the flag with every record
-- phase and sets it alone (`LabelIndex.withhold`) when the entry leaves the tier, and every read of
-- the index (the facet selection, both coverage reads, the labeller's stale scan) skips the row. A
-- corpus row has no entry and is never withheld.
--
-- **`DEFAULT false` is not true of every existing row**, so the block below derives the flag from
-- `reaction_records`, which keeps both facts, by the rule `ReactionRecordStore.structurally_withheld`
-- reads. It runs once per registered tenant with `app.tenant` set, because a forced row-level-security
-- policy (`131`) shows a migration role without a tenant no rows at all, and it restores the setting
-- it found.
--
-- **Rolling back keeps working and leaves the flag unmaintained.** The pre-135 image never names the
-- column: it serves every row, as it always did, and it never clears the flag either, so an entry
-- re-published or amended back to structured during the window stays out of the facet answers until
-- its next amendment. Re-running the block below by hand sets and clears every ELN row's flag from
-- its record.
ALTER TABLE reaction_labels ADD COLUMN IF NOT EXISTS withheld BOOLEAN NOT NULL DEFAULT false;

DO $$
DECLARE
    found_tenant text := coalesce(current_setting('app.tenant', true), '');
    each_tenant text;
BEGIN
    FOR each_tenant IN SELECT id FROM tenants ORDER BY id LOOP
        PERFORM set_config('app.tenant', each_tenant, true);
        UPDATE reaction_labels AS l
           SET withheld = (r.retracted_at IS NOT NULL OR r.tier <> 'structured')
          FROM reaction_records AS r
         WHERE r.ingest_source = l.source
           AND r.reaction_id = l.reaction_id
           AND r.tenant_id = l.tenant_id
           AND l.withheld <> (r.retracted_at IS NOT NULL OR r.tier <> 'structured');
    END LOOP;
    PERFORM set_config('app.tenant', found_tenant, true);
END
$$;
