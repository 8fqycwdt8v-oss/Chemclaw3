-- Migration ledger: one row per applied `infra/sql/*.sql` file. The runner
-- (`calc.migrate`) creates this first (it is the tracker, so it is not itself
-- tracked) and then records every other file it applies, skipping any already
-- present. `checksum` is the SHA-256 of the file text at apply time, so editing a
-- file that has already run is detected as drift rather than silently ignored.
-- Numbered 000 so it sorts before every real migration.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
