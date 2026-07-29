-- Add the fingerprint-definition column to already-created fingerprint tables (deep-review
-- follow-up, D-031). Fresh databases get the column straight from 002/003; this migration
-- brings an existing dev database up to date. Idempotent (ADD COLUMN IF NOT EXISTS), so
-- re-running `make db-migrate` is safe.
--
-- Existing rows predate the column, so they are backfilled to the v1 default definitions
-- (Morgan radius 2 / DRFP folded to 2048 — the shipped `settings.ecfp_*`/`drfp_bits`
-- defaults). If the deployment ran with non-default fingerprint config, re-index after this
-- migration so the rows carry their true definition; a mismatched backfill only makes stale
-- rows fall out of similarity search (safe), never returns a wrong score.
ALTER TABLE molecule_fingerprints
    ADD COLUMN IF NOT EXISTS definition TEXT NOT NULL DEFAULT 'ecfp:r2:b2048';

ALTER TABLE reaction_fingerprints
    ADD COLUMN IF NOT EXISTS definition TEXT NOT NULL DEFAULT 'drfp:b2048';
