-- High-water cursor for the durable ELN sync (plan step 4.5, scheduled-run seam). One row
-- per sync source: the newest entry timestamp already ingested. A Schedule-driven run loads
-- this, syncs everything newer, and stores the advanced value, so each firing is
-- self-contained and the Schedule carries no state in its payload. Idempotent ingestion makes
-- a re-fetch at the boundary harmless. `make db-migrate` applies it (tracked, idempotent).
CREATE TABLE IF NOT EXISTS sync_cursors (
    source     TEXT PRIMARY KEY,
    cursor     TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
