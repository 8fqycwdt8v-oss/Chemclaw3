-- More keys that two tenants can both produce, widened when tenancy is switched on
-- (D-2026-10-15-tenancy-is-a-column-and-a-policy-switched-on-by-the-operator).
--
-- 131 widened the keys built from a hash or a note slug. The same collision exists wherever a key
-- is something a tenant did not mint: a content hash (`document_chunks.doc_id`), a name a data
-- source carries (`source`, `ingest_source`: two releases built from one image use the same source
-- names, and so do two tenants of one release), a person (`behaviour_proposals`, `subscriptions`:
-- one person can work in two tenants) or a note slug (`note_proposals`). Without the tenant in the
-- key the second tenant's INSERT either violates the first's row, which row-level security makes
-- invisible to it (an existence oracle), or is silently dropped by an `ON CONFLICT DO NOTHING`.
--
-- Same mechanism and the same promise as 131: nothing changes until `chemclaw_apply_tenancy(true)`
-- runs, the key then gains `tenant_id` LAST, foreign keys that point at it follow, and every store
-- names the key by constraint (`ON CONFLICT ON CONSTRAINT <table>_pkey`) or by no target at all, so
-- one statement is right under either key. The expression index on `subscriptions` cannot be a
-- constraint; it is rebuilt with the tenant appended, and its one statement names no target.
--
-- A design id is a hash of the ask and its owner, so one person drafting the same experiment in two
-- tenants files the same id twice: `experiment_protocols` and its revisions are widened, and the
-- foreign keys from the revisions, status events and arm results follow the first.
--
-- Not widened, each because the key is minted here and so unique across tenants: sessions, turns,
-- jobs (a workflow id, scoped to its tenant), exhibits, effects and requests (the same), plans and
-- approvals (identifiers this system makes);
-- `session_events.dedupe_key` (starts with a Temporal workflow id and run id, and run ids are
-- unique); `bo_suggestions (campaign_id, job_id)` (the job id is a scoped workflow id).
--
-- Locks: the same as 131. Switching on takes ACCESS EXCLUSIVE on each table below and rebuilds its
-- key; all are small relative to the fingerprint and note-index tables 131 already names, except
-- `document_chunks`, which can be pre-built (`document_chunks_tenant_key`, see 131's header).
--
-- Re-runnable: every function is CREATE OR REPLACE and every step checks what it would change.

-- Rebuilds a unique index that is not a constraint (an expression or partial index) with
-- `tenant_id` appended, from its own definition. Does nothing when it already ends in the tenant.
CREATE OR REPLACE FUNCTION chemclaw_widen_index(idx text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    def text;
BEGIN
    IF to_regclass(idx) IS NULL THEN
        RAISE EXCEPTION 'cannot widen index %: not on the search_path', idx;
    END IF;
    def := pg_get_indexdef(to_regclass(idx));
    IF def ~ 'tenant_id\)' THEN
        RETURN;
    END IF;
    EXECUTE format('DROP INDEX %s', to_regclass(idx));
    EXECUTE regexp_replace(def, '\)(\s*(WHERE .*)?)$', ', tenant_id)\1');
END
$$;

CREATE OR REPLACE FUNCTION chemclaw_apply_tenancy(enabled boolean) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    keyed text[] := ARRAY[
        'artifact_blobs', 'bo_campaigns', 'budget_usage', 'calculation_artifacts',
        'calculation_claims', 'calculation_results', 'composed_workflows', 'corpus_molecules',
        'measurements', 'molecule_fingerprints', 'note_index', 'observations', 'structures',
        'tool_result_blobs', 'user_preferences',
        'commitments', 'corpus_cursors', 'corpus_reactions', 'document_chunks', 'document_files',
        'ingest_rejections', 'reaction_fingerprints', 'reaction_labels', 'reaction_records',
        'reaction_species', 'sync_cursors', 'experiment_protocols',
        'experiment_protocol_revisions'
    ];
    t text;
    rel record;
    changed integer := 0;
BEGIN
    IF NOT enabled AND (SELECT count(*) FROM tenants) > 1 THEN
        RAISE EXCEPTION 'tenancy cannot be switched off while % tenants are registered',
            (SELECT count(*) FROM tenants);
    END IF;
    IF enabled THEN
        FOREACH t IN ARRAY keyed LOOP
            PERFORM chemclaw_widen_key(t);
        END LOOP;
        PERFORM chemclaw_widen_key('predictions', 'predictions_identity');
        PERFORM chemclaw_widen_key('result_publications', 'result_publications_identity');
        PERFORM chemclaw_widen_key('note_proposals', 'note_proposals_version_unique');
        PERFORM chemclaw_widen_key('behaviour_proposals', 'behaviour_proposals_version_unique');
        PERFORM chemclaw_widen_index('subscriptions_identity');
    END IF;
    FOR rel IN
        SELECT c.oid::regclass AS name, c.relrowsecurity AS on_, c.relforcerowsecurity AS forced
          FROM pg_policy p
          JOIN pg_class c ON c.oid = p.polrelid
         WHERE p.polname = 'tenant_isolation' AND to_regclass(c.relname::text) = c.oid
         ORDER BY c.relname
    LOOP
        IF enabled AND NOT (rel.on_ AND rel.forced) THEN
            EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', rel.name);
            EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', rel.name);
            changed := changed + 1;
        ELSIF NOT enabled AND (rel.on_ OR rel.forced) THEN
            EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', rel.name);
            EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', rel.name);
            changed := changed + 1;
        END IF;
    END LOOP;
    RETURN changed;
END
$$;
