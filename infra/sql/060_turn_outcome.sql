-- How a turn ended, and what it did — the record `turn_costs.completed` collapsed into a boolean.
--
-- `turn_costs` knew exactly how every turn went and persisted five spend numbers plus one bool.
-- That bool is `answered`, so **eight distinct endings arrived as two**: a turn stopped by the
-- runaway loop cap, one that produced no prose at all, one that raised, one the wall clock killed
-- and one the client abandoned were all simply `completed = false`, while a partial answer after
-- the cap sat at `completed = true` beside a clean one. Every one of those distinctions was
-- already decided inside `chemclaw.api.runner` — read, acted on, sent to the chemist, and dropped.
--
-- The other half of the same gap: a healthy turn emitted no log record at all
-- (`grep -c logger.info api/runner.py` was 0), so a deployment without this table had no record of
-- a turn in any form. The runner now emits `turn.started`/`turn.finished` carrying the same fields
-- these columns hold, which is what makes a log-only deployment answerable too.
--
-- Additive and defaulted throughout, per `infra/sql/README.md`: every existing row keeps its
-- meaning, the previous image can still write (nothing here is `NOT NULL` without a default), and
-- `completed` stays exactly where it was because dashboards read it. It is now *derived* from
-- `outcome` by the one writer, so the two cannot disagree.

-- One of `chemclaw.api.runner._OUTCOMES`, written by `_settle_outcome` and nothing else:
-- `answered` / `loop_capped` / `empty_answer` / `errored` / `timed_out` / `abandoned`.
--
-- **No CHECK constraint, deliberately.** The vocabulary is enforced in the one function that
-- produces it, and a database constraint would make adding an outcome a migration that the
-- *previous* image cannot write through — the rollback break `README.md` requires an ADR for. The
-- cost of not constraining it is a typo in one Python literal, which `mypy` and this repository's
-- own tests are better placed to catch than Postgres is.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'unknown';

-- The user-facing classification of a failed turn (`_classify`: `storage_unavailable`,
-- `llm_timeout`, `bad_tool_arguments`, `internal`). Empty for every other outcome. It was computed
-- on every failure and sent to the chemist, so a bug report could quote a code the deployment
-- itself had no record of.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';

-- The model id the turn's **agent** route resolved to.
--
-- `core/metrics.py` and `docs/guides/runbook.md` both state that per-model attribution lives in
-- this table — it is the stated reason the spend counters deliberately carry `profile` and not
-- `model` (D-2026-08-01-spend-is-a-ledger-not-a-label) — and the table had no such column and no
-- writer. A documented attribution nothing can write is not an attribution
-- (D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution).
--
-- One turn can span models: the verifier's judge runs on its own `verifier` route and its tokens
-- are metered into the same row. This names the model that produced the answer, rather than
-- growing a column per route.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';

-- What the turn actually did. `duration_seconds` alone cannot separate a slow turn that made two
-- tool calls from a slow turn that made forty, and those are different problems with different
-- fixes. Nullable rather than `DEFAULT 0`, because a row written before these existed genuinely
-- did not count — and a fabricated zero would be indistinguishable from a turn that used no tools.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS tool_calls INT;
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS tool_failures INT;
-- Calls a governance gate stopped (the plan gate). Separate from `tool_failures` because a refusal
-- is the control working: folding the two together reports a correctly-gated turn as a broken one,
-- which is the exact mistake `ToolFailedEvent.reason` was added to prevent.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS tool_refusals INT;
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS jobs_started INT;

-- Seconds from the turn's first statement to the first token the chemist saw. **The latency a
-- chemist actually experiences**, and nothing measured it: `chemclaw_turn_duration_seconds` covers
-- the whole turn, so a turn that spent 40 s on tools and then streamed instantly and one that
-- stalled 40 s before its first word were the same sample. NULL means no token was ever produced,
-- which is a different fact from zero and is stored as one.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS ttft_seconds DOUBLE PRECISION;

-- The order the outcome question is asked in: "what failed, most recently first". Without it,
-- "show me today's `errored` turns" is a sequential scan of the whole ledger, which is the one
-- table that grows with every turn the deployment has ever taken.
CREATE INDEX IF NOT EXISTS turn_costs_outcome_idx ON turn_costs (outcome, recorded_at DESC);
