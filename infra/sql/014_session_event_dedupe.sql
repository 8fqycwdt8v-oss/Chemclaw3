-- Idempotent push-back inserts (workflows.notify): the recording activity runs at-least-once,
-- so a retry after a committed-but-unacked insert would duplicate the notification (the tailer
-- would wake the session twice for one job, and the operator channel would show one drift
-- regression as two alerts). `dedupe_key` is the writer's deterministic identity for one logical
-- event — derived in workflow code from workflow id + run id + kind + a payload digest — and the
-- partial unique index makes the retried INSERT a no-op (`ON CONFLICT DO NOTHING`). NULL keys
-- (writers with no retry semantics) keep the plain append, so existing rows need no backfill.
ALTER TABLE session_events ADD COLUMN IF NOT EXISTS dedupe_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS session_events_dedupe_idx
    ON session_events (dedupe_key)
    WHERE dedupe_key IS NOT NULL;
