-- AG-14: stamp the deployment revision (Git SHA / image digest) onto each audit row, so a past
-- agent result ties to the exact prompt/skill/config version that produced it (GxP reproducibility).
-- A pure additive column: NOT NULL with a default so existing rows read 'unknown' and new rows carry
-- the ambient `deployment_revision`. Idempotent (IF NOT EXISTS), like every migration — the runner
-- may re-apply it on a current DB. Past rows are intentionally not backfilled (they predate the
-- field; their revision is genuinely unknown).
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS revision TEXT NOT NULL DEFAULT 'unknown';
