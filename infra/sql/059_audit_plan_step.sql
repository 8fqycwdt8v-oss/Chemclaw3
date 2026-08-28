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

-- **This index build blocks every audit INSERT while it runs, and this deployment has to plan for
-- that.** `CREATE INDEX` takes a `SHARE` lock, which conflicts with the `ROW EXCLUSIVE` every
-- INSERT needs, and `audit_events` is the one table `durable/retention.py` refuses to prune: it
-- grows by one row per tool call forever, so the build gets slower for the life of the
-- deployment and never faster. Measured on this repository's own Postgres image, over a table
-- built with this one's shape: **1.24 s per million rows** (1M rows, 111 MB, warm cache) — so a
-- fleet with a hundred million audited calls stalls its agent for about two minutes, and a cold
-- or contended disk is worse.
--
-- `CONCURRENTLY` is not available *here*: `core/migrate.py` runs the whole migration set inside
-- one transaction (it takes `pg_advisory_xact_lock` to serialize migrators), and Postgres refuses
-- `CREATE INDEX CONCURRENTLY` inside a transaction block. Moving the index out of the migration
-- set instead would mean an index that exists on whichever deployments remembered to run a
-- script, which is worse than a stall nobody planned: the query it serves would be a sequential
-- scan on some pods and not others, and nothing would say which.
--
-- So the requirement is stated rather than engineered around, and it has an escape hatch that
-- costs nothing. **On a deployment whose `audit_events` is already large, build the index
-- concurrently before deploying** —
--
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_events_tool_outcome_ts_idx
--         ON audit_events (tool, outcome, ts);
--
-- — outside any transaction, on the live database. The `IF NOT EXISTS` below then finds it and
-- does nothing, so the migration is a no-op and the deploy needs no window at all. That works
-- because every `CREATE INDEX` in this directory is `IF NOT EXISTS`
-- (`tests/test_agent_review_guards.py` holds it, in both directions); a bare `CREATE INDEX` would
-- turn the pre-build into a duplicate-index error and take the escape hatch away.
--
-- Otherwise: apply this in a maintenance window, or accept the stall measured above.

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
