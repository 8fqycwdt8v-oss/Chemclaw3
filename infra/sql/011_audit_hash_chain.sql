-- RETIRED. Adds `prev_hash` and `row_hash` to `audit_events` for a tamper-evident hash chain, in
-- which each row committed a hash of the one before it so that modifying, reordering or
-- interior-deleting a row broke the chain detectably.
--
-- The chain, its signed anchors (032), its verifier and `make audit-verify` were built for a
-- regulated deployment and have since been removed: Chemclaw is not one, and the chain cost a
-- serializing advisory lock on every audit write plus a key to manage. What remains is the
-- INSERT-only grant in `grants/app_privileges.sql`, which prevents the rewrite the chain merely
-- detected.
--
-- The statements below are unchanged because the schema is forward-only — a migration may not drop
-- a column (`tests/test_migrations_are_additive.py`), and re-running this file on an existing
-- database must stay a no-op. Both columns default to '', so the writer simply stopped supplying
-- them; every row written since carries those defaults and means nothing by them.
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS prev_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS row_hash  TEXT NOT NULL DEFAULT '';
