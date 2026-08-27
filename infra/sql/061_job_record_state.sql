-- How a durable run *ended*, and why when it ended badly
-- (D-2026-08-27-a-job-that-fails-leaves-no-row).
--
-- `job_records` held one row per **completed** run and nothing else, because the only writer was
-- `ConnectorJobWorkflow._finish` and a failing job raises before it. Measured live on 2026-08-27:
-- one `ConnectorJobWorkflow` run twice, once succeeding and once failing on a `ValueError`, left
-- **one row for two jobs** — the failed one had none. So the flagship interaction had no success
-- rate, no failure rate and no error budget, and "all my CREST jobs are failing" and "nobody is
-- running jobs" were the same picture in this table and on every dashboard reading it.
--
-- `state` is the discriminator: `completed` or `failed`. It defaults to `completed` because that
-- is what every row written before this migration is — the table could not hold anything else —
-- so an existing row reads correctly rather than as "the run did not say", which is the contract
-- `payload_kind` and `plan_step` set for a fact that genuinely was not recorded.
--
-- `failure_reason` is the application's own account of the failure, from
-- `connector_job.py::failure_reason` — the *first application-level* frame of Temporal's nested
-- chain, which is the sentence the product wrote for the chemist ("unknown ALPB solvent
-- '2-methyltetrahydrofuran'; common valid names are …") rather than the library internals below it
-- or the structural "Child Workflow execution failed" above it. Empty for a run that succeeded,
-- which is the honest value: there is no reason to give.
--
-- Its own column rather than folded into `summary`: `summary` is the one line a listing shows for
-- what a run *produced*, and a listing that cannot tell a result from a failure is the ambiguity
-- this pair exists to remove.
--
-- Additive with defaults, so the previous image keeps writing this table unchanged
-- (`tests/test_migrations_are_additive.py`). Applied by `make db-migrate` (idempotent).
ALTER TABLE job_records
    ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE job_records
    ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
