-- (Numbering note: there is no 005 migration — it never existed, a renumber artifact. The runner
-- discovers migrations by filename glob, so the gap is harmless; do not backfill a 005.)
--
-- GxP tool-audit trail (agents.audit). One append-only row per agent tool call:
-- who ran what (actor), in which conversation (correlation_id), with which arguments,
-- the outcome and a short effect summary (e.g. the PR ref a propose_* tool returned),
-- and the latency. The stdlib log is the floor; this is the durable, queryable record.
--
-- Append-only by contract: the writer (agents.audit_store.PostgresAuditSink) only
-- inserts. `actor` is a Phase-6 seam — 'unknown' until Entra identity (oid/upn) is wired,
-- a value change then, not a schema change. The tamper-evident hash chain over rows
-- (prev_hash/row_hash) is added by 011_audit_hash_chain.sql (F10-G1).
CREATE TABLE IF NOT EXISTS audit_events (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ      NOT NULL DEFAULT now(),
    correlation_id TEXT             NOT NULL,
    actor          TEXT             NOT NULL,
    tool           TEXT             NOT NULL,
    arguments      TEXT             NOT NULL,
    outcome        TEXT             NOT NULL,
    detail         TEXT             NOT NULL DEFAULT '',
    latency_ms     DOUBLE PRECISION NOT NULL
);

-- Query by conversation (reconstruct one turn) and by time (a period's activity).
CREATE INDEX IF NOT EXISTS audit_events_correlation_idx ON audit_events (correlation_id);
CREATE INDEX IF NOT EXISTS audit_events_ts_idx ON audit_events (ts);
