-- The runtime principal's privileges (D-2026-08-05-append-only-by-grant-not-by-contract).
--
-- `006_audit_events.sql` calls that table "append-only by contract", and until this file the
-- contract had no enforcement of any kind: no GRANT, no REVOKE, no trigger and no second role in
-- any of the 36 migrations. One DSN with full DDL and DML was mounted on every pod, so the
-- credential that runs a chat turn could also rewrite the audit trail whose whole purpose is to
-- record what that turn did. A hash chain (011) and signed anchors (032) detected that afterwards;
-- nothing prevented it. Those are gone now, so this file is the whole of the guarantee.
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

    -- Append-only, and by grant rather than by convention. INSERT with no UPDATE and no DELETE is
    -- the whole of the trail's integrity claim: the credential that writes a row cannot rewrite or
    -- remove it. A per-row hash chain and signed anchors used to sit on top of this and made
    -- tampering *detectable*; they were built for a regulated deployment, have been removed, and
    -- this grant is what always did the preventing.
    --
    -- `audit_anchors` is deliberately absent. The table still exists — the schema is forward-only —
    -- but the code that wrote it went with the chain, and a grant nobody exercises is a privilege
    -- that only matters when someone else exercises it. `tests/test_database_privileges.py` derives
    -- this matrix from the SQL the code actually issues and fails in *both* directions, which is
    -- what caught the grant outliving its writer.
    EXECUTE format('GRANT INSERT ON audit_events TO %I', app_role);

    -- Insert and upsert, no delete. These include the three tables `durable/retention.py`
    -- explicitly refuses to prune, each for a stated reason (the cache is bounded by cost policy
    -- rather than by a clock, D-011; a job record is the durable evaluation record D-157 exists to
    -- keep). Withholding DELETE makes those refusals enforced rather than merely intended.
    EXECUTE format(
        'GRANT INSERT, UPDATE ON '
        'calculation_results, calculation_artifacts, job_records, '
        'bo_campaigns, measurements, predictions, note_proposals, observations, '
        'reaction_records, experiment_protocols, '
        'plan_approvals, sync_cursors, turn_costs, '
        'molecule_fingerprints, reaction_fingerprints, reaction_labels, corpus_molecules, '
        'corpus_reactions, corpus_cursors, '
        'tool_result_links TO %I', app_role);
    -- `tool_result_links` joins that list and `tool_result_blobs` the full-DML one below, even
    -- though retention deletes only the blob: a cascading delete is performed with the referencing
    -- table's owner privileges, not the deleting role's, so the link rows go without DELETE ever
    -- being granted on them. Withholding it is not a formality — it is what keeps "the sweep
    -- deletes blobs and links follow" the only way a link row can disappear.
    -- Insert only: written once and never revised. `bo_suggestions` is a campaign's history, and
    -- the sequence *is* the history (031), so an UPDATE would rewrite it. A geometry is the same
    -- shape one table over: `structures` is content-addressed, so the key *is* the content and a
    -- row has nothing an update could correct (D-2026-08-21).
    --
    -- `chemclaw.cli.rekey_campaigns` does UPDATE `bo_suggestions` and DELETE from `bo_campaigns`,
    -- and is deliberately absent from this matrix: it is an operator's one-off re-key run beside
    -- `make db-migrate` under the owning principal, and granting a chat turn those verbs for the
    -- life of the deployment is exactly what the withholding above is for.
    -- `tests/test_database_privileges.py` names the module and this reason.
    -- `experiment_protocol_revisions` joins them for the same reason and it is load-bearing rather
    -- than tidy: a revision is what an expert's edit to a generated protocol *is*, so a credential
    -- that could UPDATE one could rewrite the correction it exists to record. The header row
    -- (`experiment_protocols`) is upsertable above because its `head_revision`/`status` genuinely
    -- move; the revisions themselves never do.
    EXECUTE format(
        'GRANT INSERT ON bo_suggestions, structures, experiment_protocol_revisions TO %I', app_role
    );

    -- Insert, delete, and now a narrow update. The row is still written once by its creator
    -- (`ON CONFLICT DO NOTHING`, first writer wins), and offboarding removes a departed person's
    -- ownership rows along with the sessions they key (`chemclaw.agent.leaver`), which is the
    -- DELETE. The UPDATE is `set_title_if_absent`: a session is named after its opening question,
    -- and that name is not known until the first turn arrives, so it cannot be part of the insert.
    -- Guarded by `title IS NULL` in the statement itself, so the privilege is wider than the write
    -- — SQL has no column-level "only while null" — which is the usual shape and the reason this
    -- group is still spelled out on its own line rather than folded into the full-DML list below.
    EXECUTE format('GRANT INSERT, UPDATE, DELETE ON session_owners TO %I', app_role);

    -- The result outbox joins this group and exercises all three verbs: INSERT on enqueue, UPDATE
    -- to mark a row delivered or to count a failed attempt, and DELETE because retention prunes
    -- *delivered* rows. That last one is the difference between this table and the two next to it
    -- that refuse pruning: a delivered row is a receipt for something that now lives in two
    -- places, so keeping every one forever would be a third copy of every result this deployment
    -- has ever computed. A pending or failed row is never pruned - it is the only record that
    -- something has *not* been published.
    --
    -- Full DML, because the application genuinely deletes from these: the retention sweep prunes
    -- conversation history and spent mailbox rows, artifact eviction reclaims cold blobs, a turn
    -- claim is released, a subscription is removed, a preference is unset. The two document tables
    -- join them because the share index is derived from a filesystem it does not own: a file
    -- deleted from the share has to leave the index, or a chemist keeps being cited a document
    -- nobody can open.
    --
    -- `note_index` joins them on that same argument, and moved here from the insert/update list
    -- above when `reindex_notes` gained a prune (D-2026-08-25). It is derived from the Git note
    -- tree exactly as the document tables are derived from the share, and until then a note deleted
    -- from the tree left its row — and, once the dense half can live in an external store, its
    -- vector — behind forever.
    --
    -- `reaction_species` joins them one level down: an amended entry that removed a charge leaves a
    -- higher ordinal behind, and without the DELETE the label index keeps answering "this reaction
    -- used TEA" from a species the current record no longer has. `reaction_labels` itself stays in
    -- the insert/update group above — a reaction is never unrecorded, only re-recorded.
    --
    -- `ingest_rejections` is the one table whose DELETE is not a retention sweep's and not a
    -- derived index's: the ledger bounds *itself*, evicting the least recently refused row of a
    -- source inside the same transaction that writes a new one
    -- (D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask). A corpus with one
    -- systematically broken field would otherwise write a row per record per run, and no sweep
    -- runs often enough to be the answer to that. The eviction is the only DELETE any code issues
    -- against it.
    EXECUTE format(
        'GRANT INSERT, UPDATE, DELETE ON '
        'session_messages, session_events, session_turns, subscriptions, user_preferences, '
        'artifact_blobs, document_files, document_chunks, note_index, tool_result_blobs, '
        'reaction_species, result_publications, ingest_rejections TO %I', app_role);

    -- The tables LangGraph creates for itself, which no migration in `infra/sql` declares and which
    -- therefore fell through every enumeration above until they were named here.
    --
    -- **Why this block has to exist, and why it is guarded.** `REVOKE ALL ON ALL TABLES` two dozen
    -- lines up is indiscriminate — it reaches these too — and `GRANT SELECT ON ALL TABLES` then
    -- hands back read and nothing else. The first install survives that because the tables do not
    -- exist yet when this file runs: the app pods create them lazily, as owner, on first turn. The
    -- *second* `helm upgrade` re-runs this file (`migrate-job.yaml`, every release, deliberately)
    -- with the tables present, and the REVOKE materialises an ACL that strips even the owner's own
    -- DML. Every turn then fails at its first checkpoint write, and so do `agent/leaver`'s erasure
    -- and `durable/retention`'s checkpoint sweep. It is a second-deploy outage that a first deploy
    -- cannot reveal, which is exactly why it is written down here rather than left to the REVOKE.
    --
    -- Guarded on existence rather than granted unconditionally, because a fresh database really
    -- does not have them yet and a `GRANT` on a missing table aborts the whole block. That makes
    -- this the one group whose grant lands on the *second* run — the same run that first needs it.
    --
    -- The verbs are upstream's, read off the installed distributions rather than assumed:
    -- `checkpoints`/`checkpoint_writes`/`store`/`store_vectors` upsert (`ON CONFLICT … DO UPDATE`),
    -- `checkpoint_blobs` does not (`DO NOTHING`), and the three `*_migrations` version tables are
    -- append-only ledgers `setup()` writes one row into per schema step. The DELETEs are ours:
    -- retention prunes by thread and erasure clears a departed person's turn state.
    -- `tests/test_database_privileges.py` derives the same table set from upstream and fails if this
    -- list drifts from it, in either direction.
    -- Guarded **per table**, not per `setup()` group. The tables of one group are created together
    -- in practice, so a group guard would usually do — but "usually" is the wrong standard here: a
    -- `GRANT` naming a table that does not exist raises, and a raise anywhere in this block aborts
    -- the whole reconciliation, so one interrupted `setup()` would leave *every* table in this file
    -- ungranted. The vector pair genuinely is absent on this deployment (the store is built with no
    -- `index_config`), which makes the independent guard load-bearing rather than defensive.
    --
    -- The table names are spelled literally rather than looped over, so the same regex that reads
    -- every other grant in this file reads these too.
    IF to_regclass('public.checkpoints') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT, UPDATE, DELETE ON checkpoints TO %I', app_role);
    END IF;
    IF to_regclass('public.checkpoint_writes') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT, UPDATE, DELETE ON checkpoint_writes TO %I', app_role);
    END IF;
    IF to_regclass('public.checkpoint_blobs') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT, DELETE ON checkpoint_blobs TO %I', app_role);
    END IF;
    IF to_regclass('public.checkpoint_migrations') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT ON checkpoint_migrations TO %I', app_role);
    END IF;
    IF to_regclass('public.store') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT, UPDATE, DELETE ON store TO %I', app_role);
    END IF;
    IF to_regclass('public.store_migrations') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT ON store_migrations TO %I', app_role);
    END IF;
    IF to_regclass('public.store_vectors') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT, UPDATE, DELETE ON store_vectors TO %I', app_role);
    END IF;
    IF to_regclass('public.vector_migrations') IS NOT NULL THEN
        EXECUTE format('GRANT INSERT ON vector_migrations TO %I', app_role);
    END IF;

    -- Sequences for every table the role may INSERT into (BIGSERIAL needs USAGE on its sequence).
    -- All of them rather than a list: a sequence confers no read of any table's rows, and an
    -- enumerated list would be a third place the table set is written down.
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO %I', app_role);

    -- `schema_migrations` is deliberately absent from every GRANT above: the ledger is the
    -- migrator's record of its own work, and a runtime credential able to write it could mark a
    -- migration applied that never ran.
END
$$;
