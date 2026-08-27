-- The plan step an audited tool call served, and the index that makes the trail queryable by
-- failure (D-2026-08-27-a-refusal-is-not-a-crash).
--
-- `plan_step` is the same fact `job_records.plan_step` carries one migration back
-- (057_job_plan_step.sql) — the `content` of the first `in_progress` todo at the moment of the
-- call — and it is here because a durable job is the *rare* case: `audit_events` holds a row for
-- every tool call, most of which launch nothing. The stamp was already computable at that instant
-- and was thrown away, so `chemclaw explain` could say which tools ran for a turn and never which
-- step of the plan each one served.
--
-- Empty for every row written before this migration, and for every call not made from a plan step
-- (a profile without the harness, a subagent, a template step, the CLI) — the same contract
-- `job_records.plan_step` set, which reads correctly as "the call did not say".
ALTER TABLE audit_events
    ADD COLUMN IF NOT EXISTS plan_step TEXT NOT NULL DEFAULT '';

-- "Show me every failing tool this week" was a sequential scan on a table that grows by one row
-- per tool call forever: the trail's only indexes are `correlation_id` (reconstruct one turn) and
-- `ts` (a period's activity), and neither helps a predicate on `tool` or `outcome`.
--
-- The column order is the query's. `tool` first because it is the most selective and the one a
-- reader always fixes ("why is predict_pka failing"); `outcome` second, now that it separates a
-- refusal from a crash rather than pooling them; `ts` last so the range predicate is the trailing
-- one the index can still bound, and so `ORDER BY ts` over a fixed tool and outcome comes back
-- ordered without a sort.
CREATE INDEX IF NOT EXISTS audit_events_tool_outcome_ts_idx
    ON audit_events (tool, outcome, ts);
