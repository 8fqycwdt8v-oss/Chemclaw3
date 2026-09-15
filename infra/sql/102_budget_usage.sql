-- The per-user spend window that survives a restart and is shared across pods
-- (`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`). Closes the `DEFERRED.md` row
-- "Durable / rolling-window budget quota", which named this design: "back the counters with a
-- Postgres table and a windowed reset, reusing the same `check`/`record` seam".
--
-- **One row per principal, reset in place rather than appended**, which is the whole reason this
-- table needs no retention window. A window-partitioned key (`actor, window_start`) would grow one
-- row per user per window forever and would have to be swept; resetting in place bounds the table
-- by the number of distinct principals the deployment has ever served, which is the same bound
-- `budget_max_tracked_users` already names for the in-process map this backs. The reset is lazy and
-- atomic: `api/budget_store.py` does it inside the upsert's `ON CONFLICT` arm, so a stale row and a
-- fresh one are one statement apart and no sweep is involved.
--
-- **Only the user scope.** A session budget stays in-process deliberately: a session is bounded by
-- `service_max_live_sessions` and dies with the process, so a durable counter for it would outlive
-- the thing it meters. The deferral asked for per-*user* fairness across restarts and pods, and
-- that is a property of a principal rather than of a conversation.
--
-- `turns` and `tokens` are BIGINT because `budget_max_tokens_per_user` defaults to 20,000,000 and a
-- deployment may raise it; INTEGER would overflow inside three orders of magnitude of the default.
CREATE TABLE IF NOT EXISTS budget_usage (
    actor        TEXT        PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    turns        BIGINT      NOT NULL DEFAULT 0,
    tokens       BIGINT      NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT budget_usage_counts_are_not_negative CHECK (turns >= 0 AND tokens >= 0)
);
