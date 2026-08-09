-- The runtime principal's privileges (D-2026-08-05-append-only-by-grant-not-by-contract).
--
-- `006_audit_events.sql` calls that table "append-only by contract", and until this file the
-- contract had no enforcement of any kind: no GRANT, no REVOKE, no trigger and no second role in
-- any of the 36 migrations. One DSN with full DDL and DML was mounted on every pod, so the
-- credential that runs a chat turn could also rewrite the GxP trail whose whole purpose is to
-- record what that turn did. The hash chain (011) and the signed anchors (032) detect that
-- afterwards; nothing prevented it.
--
-- **Not a numbered migration, and the distinction is load-bearing.** `infra/sql/*.sql` is applied
-- exactly once per file and tracked by checksum, which is right for a schema change and wrong for
-- this in two ways at once: a deployment that creates its runtime role *after* the first
-- `db-migrate` would never have the grants applied, and every table added by a later migration
-- would ship ungranted and break the application on first use. A grant is a *reconciliation*
-- against a role that may appear at any time, over a schema that keeps growing — so it lives
-- outside the numbered set (the runner globs `infra/sql/*.sql`, not subdirectories) and
-- `make db-grants` re-applies it on every deploy, after the migrations.
--
-- The matrix below is exactly the verbs `src/` executes and nothing more.
-- `tests/test_database_privileges.py` derives that set from the SQL literals in `src/` and fails
-- if the two disagree in *either* direction — so this is a declaration checked against the live
-- surface, not a second, drifting definition of what the application does.
--
-- No-ops when the role does not exist, so a single-principal deployment (a dev database, CI,
-- `make up`) runs this and is unaffected: splitting the principal is a deployment's opt-in, and a
-- script that failed without it would make the split mandatory for everyone.
DO $$
DECLARE
    app_role CONSTANT TEXT := 'chemclaw_app';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        RAISE NOTICE 'role % does not exist; this deployment runs a single database principal '
                     '(see D-2026-08-05-append-only-by-grant-not-by-contract)', app_role;
        RETURN;
    END IF;

    -- Start from nothing rather than from whatever an earlier release left behind, so this file
    -- states the whole matrix: re-running it after a verb is *removed* from the code narrows the
    -- grant instead of leaving the old one standing.
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', app_role);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', app_role);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app_role);

    -- Read is uniform: every table the application reads, it may read. The interesting half is
    -- write, below.
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', app_role);

    -- Append-only, and now by grant. INSERT with no UPDATE and no DELETE is what makes the hash
    -- chain's assumption true rather than assumed: a chain link cannot be rewritten by the
    -- credential that writes chain links. `audit_anchors` carries the same argument — an anchor is
    -- the evidence that a *trailing* truncation happened
    -- (D-2026-08-01-a-restore-is-a-truncation-nobody-can-see), so an actor able to delete anchors
    -- could hide the one alteration the chain by itself cannot see.
    EXECUTE format('GRANT INSERT ON audit_events, audit_anchors TO %I', app_role);

    -- Insert and upsert, no delete. These include the three tables `durable/retention.py`
    -- explicitly refuses to prune, each for a stated reason (the cache is bounded by cost policy
    -- rather than by a clock, D-011; a job record is the durable evaluation record D-157 exists to
    -- keep). Withholding DELETE makes those refusals enforced rather than merely intended.
    EXECUTE format(
        'GRANT INSERT, UPDATE ON '
        'calculation_results, calculation_artifacts, job_records, '
        'bo_campaigns, measurements, predictions, note_proposals, observations, '
        'plan_approvals, note_index, sync_cursors, turn_costs, '
        'molecule_fingerprints, reaction_fingerprints, tool_result_links TO %I', app_role);
    -- `tool_result_links` joins that list and `tool_result_blobs` the full-DML one below, even
    -- though retention deletes only the blob: a cascading delete is performed with the referencing
    -- table's owner privileges, not the deleting role's, so the link rows go without DELETE ever
    -- being granted on them. Withholding it is not a formality — it is what keeps "the sweep
    -- deletes blobs and links follow" the only way a link row can disappear.
    -- Insert only: written once and never revised. `bo_suggestions` is a campaign's history, and
    -- the sequence *is* the history (031), so an UPDATE would rewrite it.
    EXECUTE format('GRANT INSERT ON bo_suggestions TO %I', app_role);

    -- Insert and delete, no update: `session_owners` upserts with `DO NOTHING` (first writer wins),
    -- so it needs no UPDATE — but offboarding removes a departed person's ownership rows along with
    -- the sessions they key (`chemclaw.agent.leaver`), so it does need DELETE. Kept on its own line
    -- because that combination is unlike every other group here, and folding it into the full-DML
    -- list below would silently hand it the UPDATE its writer deliberately does not use.
    EXECUTE format('GRANT INSERT, DELETE ON session_owners TO %I', app_role);

    -- Full DML, because the application genuinely deletes from these: the retention sweep prunes
    -- conversation history and spent mailbox rows, artifact eviction reclaims cold blobs, a turn
    -- claim is released, a subscription is removed, a preference is unset. The two document tables
    -- join them because the share index is derived from a filesystem it does not own: a file
    -- deleted from the share has to leave the index, or a chemist keeps being cited a document
    -- nobody can open.
    EXECUTE format(
        'GRANT INSERT, UPDATE, DELETE ON '
        'session_messages, session_events, session_turns, subscriptions, user_preferences, '
        'artifact_blobs, document_files, document_chunks, tool_result_blobs TO %I', app_role);

    -- Sequences for every table the role may INSERT into (BIGSERIAL needs USAGE on its sequence).
    -- All of them rather than a list: a sequence confers no read of any table's rows, and an
    -- enumerated list would be a third place the table set is written down.
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', app_role);

    -- `schema_migrations` is deliberately absent from every GRANT above: the ledger is the
    -- migrator's record of its own work, and a runtime credential able to write it could mark a
    -- migration applied that never ran.
END
$$;
