-- The plan step a durable job was launched for, and the plan revision it belonged to
-- (D-2026-08-27-a-job-names-the-step-it-serves).
--
-- A plan is the harness's todo list and a durable job is a row here; nothing joined them, so a
-- chemist watching an approved plan run could not tell a step waiting on a six-hour CREST search
-- from a plan that stalled. The join cannot live on the todo side — a marker written into a todo's
-- `content` perturbs `plan_identity` and revokes the approval keyed on it, which is why that
-- design was deleted twice — so it lives here: the launch stamps the first `in_progress` todo's
-- content and the plan's identity hash onto the run.
--
-- `plan_step` is the todo's bare content (matchable and debuggable without sharing a hash
-- function with any surface); `plan_hash` is `plan_identity` over the whole plan, so a job from a
-- superseded plan revision matches no current step by hash rather than by fuzzy text.
--
-- Empty for every row written before this migration and for every run not launched from a plan
-- step (a template step, the CLI, a turn with no plan), which reads correctly as "the run did not
-- say" — the same contract `payload_kind` set one migration back.
--
-- Additive with defaults, so the previous image keeps writing this table unchanged
-- (`tests/test_migrations_are_additive.py`). Applied by `make db-migrate` (idempotent).
ALTER TABLE job_records
    ADD COLUMN IF NOT EXISTS plan_step TEXT NOT NULL DEFAULT '';
ALTER TABLE job_records
    ADD COLUMN IF NOT EXISTS plan_hash TEXT NOT NULL DEFAULT '';
