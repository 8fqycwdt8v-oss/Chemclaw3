-- One durable row per finished connector job: what was run, on what arguments, what came out —
-- and **why it was run at all** (D-155).
--
-- Before this table a durable job's own result lived in exactly one place, the Temporal workflow
-- result. Temporal is an execution engine, not an archive: namespace retention expires a closed
-- workflow's history, and with it the entire result. For a multi-round BO campaign that meant the
-- best point *and every intermediate observation* — the expensive part — became unrecoverable on a
-- clock nobody in this repository configures. A later session could not even find the run: the job
-- id lives in the launching conversation's transcript, and `get_durable_job_status` needs that id.
--
-- `rationale` is the field the rest of the system had nowhere to put. Notes record what a job
-- produced (deliberately output-neutral, D-005) and `audit_events` records that a tool was called;
-- neither says what question the run was meant to answer, which is exactly what a chemist — or the
-- agent, months later — needs to judge whether the result still applies.
--
-- `payload` is the launch arguments (for a campaign: the whole decision space, objective, seed and
-- round count) and `result` is the job's own structured envelope, so the row is self-contained:
-- reading it back reconstructs the run without Temporal, without the session, and without the
-- knowledge graph.
--
-- Not pruned by `chemclaw.durable.retention`, and that is deliberate — see its docstring. Applied
-- by `make db-migrate`.
CREATE TABLE IF NOT EXISTS job_records (
    -- The Temporal workflow id, which is the deterministic idempotency key derived from the job
    -- and its arguments (`connectors/jobs.py::job_workflow_id`). Primary key, so the upsert a
    -- workflow retry performs updates the row rather than forking it.
    job_id         TEXT        PRIMARY KEY,
    connector      TEXT        NOT NULL,
    job            TEXT        NOT NULL,
    rationale      TEXT        NOT NULL,
    requested_by   TEXT        NOT NULL,
    session_id     TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT        NOT NULL DEFAULT '',
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    summary        TEXT        NOT NULL,
    result         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- The note this run proposed through the PR-gate, or '' when it produced none. A join to the
    -- knowledge graph, not proof of a merge: an agent note is a *proposal* until a human signs it.
    note_id        TEXT        NOT NULL DEFAULT '',
    completed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The two orders anything reads this table in: newest-first for "what have we run lately", and by
-- capability for "every campaign this connector has run".
CREATE INDEX IF NOT EXISTS job_records_completed_at_idx ON job_records (completed_at DESC);
CREATE INDEX IF NOT EXISTS job_records_connector_job_idx ON job_records (connector, job);
