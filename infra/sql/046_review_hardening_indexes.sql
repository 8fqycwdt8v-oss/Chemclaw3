-- Three independently verified gaps from a database-setup review (2026-08-16), batched into one
-- migration because each is a single small DDL statement and none depends on the others.
--
-- `session_owners_owner_idx` — `agent.session_store._OWNER_LIST` (043_session_listing) is the query
-- behind every `GET /sessions` call: `WHERE o.owner IS NOT DISTINCT FROM %s`, and `session_owners`
-- has never carried an index on `owner`. The table is explicitly never pruned (013: "survives its
-- session's pruned history"), so every session list, for every user, for the life of the
-- deployment, has been a sequential scan over a table that only grows.
--
-- `molecule_fingerprints_definition_idx` / `reaction_fingerprints_definition_idx` — both tables
-- carry `definition` specifically so "similarity search filters to one definition" (002, 003), and
-- 004 shows two definitions legitimately coexisting during a rollout. `science.fingerprints.store`
-- filters `WHERE definition = %(definition)s` for `_exists`, `_count`, and the HNSW similarity
-- search itself, and none of the three has ever had a supporting index. Once two definitions
-- coexist — the exact case 004 exists for — the ANN query combines an unindexed filter with the
-- HNSW index, the known pgvector shape where the filter narrows the ANN candidate set rather than
-- the index scan itself, and can silently hand back fewer than the requested `LIMIT`.
--
-- `session_messages_shape_known` — `message_shape` (043_session_message_shape) is enum-like in
-- every way that matters (exactly two literal values, `agent.message_migration.MAF_SHAPE` and
-- `LANGCHAIN_SHAPE`, read back by `agent.message_pairing` to decide how to parse a row) except that
-- it has never carried a `CHECK`, unlike every comparable column elsewhere in this schema
-- (`note_proposals.state`, `observations.status`/`origin`, `bo_campaigns.direction`). A stray value
-- — an application bug, a manual `UPDATE`, a future third message framework — is accepted silently
-- today and mis-parses a transcript, with nothing at the database layer to catch it before a
-- chemist sees the result.
--
-- `NOT VALID`, deliberately not followed by `VALIDATE CONSTRAINT` in this file: `core.migrate`
-- applies a whole migration file in one transaction, and Postgres never downgrades a lock mid
-- transaction, so validating here would hold the same lock for the same full-table scan a plain
-- `ADD CONSTRAINT` would — no cheaper — and `session_messages` is one of the largest, most actively
-- written tables in this schema. `NOT VALID` enforces the check on every write from this migration
-- onward at the cost of a catalog update, not a scan; every row ever written since 043 only ever
-- held `'maf'` or `'langchain'` (message_migration's only two literals, and the column's own
-- default), so the historical data already satisfies it in practice. An operator wanting the
-- database's own proof of that, rather than this migration's word for it, can run
-- `VALIDATE CONSTRAINT session_messages_shape_known` separately, at a time of their choosing, under
-- a `SHARE UPDATE EXCLUSIVE` lock that blocks neither reads nor writes.
--
-- Applied by `make db-migrate`.
CREATE INDEX IF NOT EXISTS session_owners_owner_idx
    ON session_owners (owner);

CREATE INDEX IF NOT EXISTS molecule_fingerprints_definition_idx
    ON molecule_fingerprints (definition);

CREATE INDEX IF NOT EXISTS reaction_fingerprints_definition_idx
    ON reaction_fingerprints (definition);

ALTER TABLE session_messages
    ADD CONSTRAINT session_messages_shape_known
    CHECK (message_shape IN ('maf', 'langchain')) NOT VALID;
