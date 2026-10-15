-- Tenancy: a tenant_id column and a row-level-security policy on every tenant-scoped table
-- (D-2026-10-15-tenancy-is-a-column-and-a-policy-switched-on-by-the-operator).
--
-- **Inert until an operator turns it on.** This file adds the column, the policy and the registry,
-- and does not enable the policy: a table whose policy is not enabled returns every row, so a
-- single-tenant deployment behaves exactly as it did. `core/migrate.py` calls
-- `chemclaw_apply_tenancy(settings.tenancy_enabled)` on every run, and that is what executes
-- `ENABLE` and `FORCE ROW LEVEL SECURITY`; it does nothing, and locks nothing, when the tables are
-- already in the requested state.
--
-- **The column fills itself.** `tenant_id TEXT NOT NULL DEFAULT 'default'` is added first (a
-- constant default: no rewrite, and every existing row is the default tenant), then its default
-- becomes the connection's `app.tenant` setting, falling back to 'default' where none is set. An
-- INSERT that does not name the tenant therefore takes the one the connection acts for
-- (`core/db.connection()` sets it), and the policy's WITH CHECK refuses a row for any other. The
-- previous image names no tenant and writes the default's rows, which is right while tenancy is off.
--
-- **Which tables.** Every table that holds a chemist's content, attribution or an access signal.
-- Not these, each for a reason `tests/test_tenancy_inventory.py` states and checks: the ledger
-- (`schema_migrations`), the registry below, the fleet coordination tables (`session_turns`,
-- `session_turn_remotes`, `session_turn_frames`, `request_buckets`: the deployment's ceilings and the
-- relay between replicas count across tenants and hold leases, not content), the retired
-- `audit_anchors`, and LangGraph's own tables, which no migration creates. kg_* (128) already carry
-- the column; they gain the policy and the default.
--
-- **Keys.** A key that two tenants can both produce (a content hash, a hash of a problem, a note
-- slug) must carry the tenant, or a second tenant's INSERT collides with the first's row. That is a
-- change of primary key, which ends "deploy the previous image" for the table, so this file does
-- not make it: `chemclaw_apply_tenancy(true)` does, as part of switching tenancy on, putting
-- `tenant_id` LAST in the key (a lookup by the old key still leads the index) and re-creating the
-- foreign keys that point at it over the wider key, NOT VALID and then VALIDATED. The stores name
-- these keys by constraint (`ON CONFLICT ON CONSTRAINT …_pkey`), which is the same statement under
-- either key, so the code does not change with the setting. The two unique indexes that identify a
-- row the same way (`predictions_identity`, `result_publications_identity`) are made constraints
-- here, metadata only, for the same reason. Keys a process mints (session, turn, job, exhibit,
-- design ids) are untouched: a tenant cannot collide with them.
--
-- **Locks.** Every statement below that touches a table takes ACCESS EXCLUSIVE, held to the end of
-- the migration's one transaction (`infra/sql/README.md`, "Migrations that lock a hot table"). Adding
-- the column, the policy and a constraint adopted from its index is metadata only. Switching tenancy
-- on later also takes the lock on each scoped table to enable its policy (a catalogue change), and
-- on the keyed tables rebuilds their key, a heap scan whose time grows with the table. A deployment
-- with a large `molecule_fingerprints` or `note_index` can build the new index first, outside any
-- transaction, under the name the function looks for, and the key then changes with no scan:
--     CREATE UNIQUE INDEX CONCURRENTLY molecule_fingerprints_tenant_key
--         ON molecule_fingerprints (id, definition, tenant_id);
-- (the table, and its key columns in order, followed by `tenant_id`). An invalid or differently
-- shaped index of that name is ignored and the key is built the ordinary way.
--
-- Re-runnable: every function is CREATE OR REPLACE and every step checks what it would change.

-- The tenants a deployment serves. A tenant exists because an operator registered it
-- (`python -m chemclaw.cli.tenants add <id>`), never because a token named it. Global: it defines
-- the tenancy, so it carries no policy.
CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT        PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenants_id_is_a_slug CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,62}$')
);

INSERT INTO tenants (id) VALUES ('default') ON CONFLICT (id) DO NOTHING;

-- Adds the column where it is missing, makes its default the connection's tenant, and creates the
-- policy where it is missing. Not enabled: see `chemclaw_apply_tenancy`.
CREATE OR REPLACE FUNCTION chemclaw_scope_table(tbl text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    rel regclass := to_regclass(tbl);
BEGIN
    IF rel IS NULL THEN
        RAISE EXCEPTION 'cannot scope %: no such table on the search_path', tbl;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = rel AND attname = 'tenant_id' AND NOT attisdropped
    ) THEN
        EXECUTE format('ALTER TABLE %s ADD COLUMN tenant_id TEXT NOT NULL DEFAULT %L', rel, 'default');
    END IF;
    EXECUTE format(
        'ALTER TABLE %s ALTER COLUMN tenant_id SET DEFAULT '
        || 'coalesce(nullif(current_setting(%L, true), %L), %L)',
        rel, 'app.tenant', '', 'default'
    );
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy WHERE polrelid = rel AND polname = 'tenant_isolation'
    ) THEN
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %s '
            || 'USING (tenant_id = current_setting(%L, true)) '
            || 'WITH CHECK (tenant_id = current_setting(%L, true))',
            rel, 'app.tenant', 'app.tenant'
        );
    END IF;
END
$$;

-- Puts `tenant_id` last in a key of `tbl`: its primary key, or the unique constraint `con`. Foreign
-- keys that reference the old key are re-created over the wider one. Does nothing when the key
-- already ends in `tenant_id`. Takes the `<tbl>_tenant_key` index when one was built beforehand.
CREATE OR REPLACE FUNCTION chemclaw_widen_key(tbl text, con text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    rel regclass := to_regclass(tbl);
    key_name text;
    key_kind "char";
    key_cols text[];
    key_sql text;
    prebuilt text := tbl || '_tenant_key';
    usable boolean := false;
    fk record;
    child_cols text;
    parent_cols text;
    pending jsonb := '[]'::jsonb;
    item jsonb;
BEGIN
    IF rel IS NULL THEN
        RAISE EXCEPTION 'cannot widen a key of %: no such table on the search_path', tbl;
    END IF;
    SELECT x.conname, x.contype, x.cols INTO key_name, key_kind, key_cols
      FROM (
        SELECT con2.conname, con2.contype,
               array_agg(a.attname::text ORDER BY k.ord) AS cols
          FROM pg_constraint con2
          CROSS JOIN LATERAL unnest(con2.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con2.conrelid AND a.attnum = k.attnum
         WHERE con2.conrelid = rel
           AND ((con IS NULL AND con2.contype = 'p') OR (con IS NOT NULL AND con2.conname = con))
         GROUP BY con2.conname, con2.contype
      ) AS x;
    IF key_name IS NULL OR 'tenant_id' = ANY (key_cols) THEN
        RETURN;
    END IF;
    key_sql := CASE key_kind WHEN 'p' THEN 'PRIMARY KEY' ELSE 'UNIQUE' END;

    -- Foreign keys that reference the old key come off first and are re-created after, widened.
    FOR fk IN
        SELECT c.conname, c.conrelid::regclass AS child, c.confdeltype,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS ccols,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum) AS pcols
          FROM pg_constraint c
         WHERE c.contype = 'f' AND c.confrelid = rel
    LOOP
        pending := pending || jsonb_build_object(
            'child', fk.child::text, 'name', fk.conname, 'ccols', to_jsonb(fk.ccols),
            'pcols', to_jsonb(fk.pcols), 'del', fk.confdeltype::text
        );
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', fk.child, fk.conname);
    END LOOP;

    SELECT to_regclass(prebuilt) IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_index i
         WHERE i.indexrelid = to_regclass(prebuilt) AND i.indrelid = rel
           AND i.indisunique AND i.indisvalid AND i.indpred IS NULL AND i.indexprs IS NULL
           AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum)
               = key_cols || 'tenant_id'::text
    ) INTO usable;

    IF usable THEN
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I, ADD CONSTRAINT %I %s USING INDEX %I',
            rel, key_name, key_name, key_sql, prebuilt
        );
    ELSE
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I, ADD CONSTRAINT %I %s (%s)',
            rel, key_name, key_name, key_sql,
            (SELECT string_agg(quote_ident(c), ', ') FROM unnest(key_cols || 'tenant_id'::text) AS c)
        );
    END IF;

    FOR item IN SELECT * FROM jsonb_array_elements(pending) LOOP
        SELECT string_agg(quote_ident(c), ', ')
          INTO child_cols
          FROM jsonb_array_elements_text((item -> 'ccols') || '"tenant_id"'::jsonb) AS c;
        SELECT string_agg(quote_ident(c), ', ')
          INTO parent_cols
          FROM jsonb_array_elements_text((item -> 'pcols') || '"tenant_id"'::jsonb) AS c;
        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I FOREIGN KEY (%s) REFERENCES %s (%s) ON DELETE %s NOT VALID',
            item ->> 'child', item ->> 'name', child_cols, rel, parent_cols,
            CASE item ->> 'del'
                WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT'
                WHEN 'r' THEN 'RESTRICT' ELSE 'NO ACTION' END
        );
        EXECUTE format('ALTER TABLE %s VALIDATE CONSTRAINT %I', item ->> 'child', item ->> 'name');
    END LOOP;
END
$$;

-- Makes a table's unique index a constraint of the same name, which changes no row and builds
-- nothing, so a statement can name it (`ON CONFLICT ON CONSTRAINT`) whatever columns the key has.
CREATE OR REPLACE FUNCTION chemclaw_adopt_unique_index(tbl text, idx text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    rel regclass := to_regclass(tbl);
BEGIN
    IF rel IS NULL OR to_regclass(idx) IS NULL THEN
        RAISE EXCEPTION 'cannot adopt index % of %: not on the search_path', idx, tbl;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = rel AND conname = idx) THEN
        EXECUTE format('ALTER TABLE %s ADD CONSTRAINT %I UNIQUE USING INDEX %I', rel, idx, idx);
    END IF;
END
$$;

-- The one switch. True widens the keys that two tenants could both produce, then enables and forces
-- the policies on every scoped table (FORCE, so the owner is subject to them: in a single-principal
-- deployment the runtime role is the owner). False disables the policies and leaves the keys wide,
-- which the stores' statements do not care about; it is refused while more than one tenant is
-- registered. A table already in the requested state is not touched, so an unchanged setting takes
-- no lock.
CREATE OR REPLACE FUNCTION chemclaw_apply_tenancy(enabled boolean) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    keyed text[] := ARRAY[
        'artifact_blobs', 'bo_campaigns', 'budget_usage', 'calculation_artifacts',
        'calculation_claims', 'calculation_results', 'composed_workflows', 'corpus_molecules',
        'measurements', 'molecule_fingerprints', 'note_index', 'observations', 'structures',
        'tool_result_blobs', 'user_preferences'
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

DO $$
DECLARE
    scoped text[] := ARRAY[
        'artifact_blobs', 'audit_events', 'behaviour_proposals', 'bo_campaigns', 'bo_suggestions',
        'budget_usage', 'calculation_artifacts', 'calculation_claims', 'calculation_results',
        'commitments', 'composed_workflows', 'corpus_cursors', 'corpus_molecules',
        'corpus_reactions', 'document_chunks', 'document_files', 'effects',
        'experiment_arm_results', 'experiment_protocol_revisions',
        'experiment_protocol_status_events', 'experiment_protocols', 'ingest_rejections',
        'job_records', 'kg_edges', 'kg_note_revisions', 'kg_notes', 'measurements',
        'molecule_fingerprints', 'note_index', 'note_proposals', 'observations',
        'pending_request_answers', 'pending_requests', 'plan_approvals', 'plan_authors',
        'predictions', 'reaction_fingerprints', 'reaction_labels', 'reaction_records',
        'reaction_species', 'result_publications', 'session_attachments', 'session_events',
        'session_exhibit_revisions', 'session_exhibits', 'session_members', 'session_messages',
        'session_owners', 'session_turn_queue', 'structures', 'subscriptions', 'sync_cursors',
        'tool_result_blobs', 'tool_result_links', 'turn_costs', 'user_preferences'
    ];
    t text;
BEGIN
    FOREACH t IN ARRAY scoped LOOP
        PERFORM chemclaw_scope_table(t);
    END LOOP;
    PERFORM chemclaw_adopt_unique_index('predictions', 'predictions_identity');
    PERFORM chemclaw_adopt_unique_index('result_publications', 'result_publications_identity');
END
$$;

-- The listings that take no predicate but the tenant: with the policy on, the newest-first walk of
-- a large tenant's rows would otherwise be filtered row by row to find a small tenant's.
CREATE INDEX IF NOT EXISTS experiment_protocols_tenant_recent_idx
    ON experiment_protocols (tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS job_records_tenant_completed_at_idx
    ON job_records (tenant_id, completed_at DESC);
