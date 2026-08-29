-- What this system changed in a system it does not own (F1).
--
-- **The write path was never missing; the distinction was.** `ConnectorManifest` has routed mutation
-- through `jobs:` since D-029 — "which core authorizes, dry-run-gates and attributes" — so a job
-- could always write. What nothing said was whether a job writes *this* deployment's database or
-- somebody else's system of record, and the two are not the same act: re-running a cached
-- calculation is free, and filing a deviation twice is a second deviation.
--
-- This table is the record of the second kind. `job_records` says a run happened and what it
-- returned; this says what changed outside, whether it can be undone, and who approved it when it
-- could not.
--
-- **Written before the effect is attempted and updated after**, which is the whole design. A row in
-- `attempting` after a crash is the honest state: this system may have changed something and cannot
-- prove it did not. A ledger that only recorded successes would answer "nothing happened" for
-- exactly the case an operator most needs to investigate.
CREATE TABLE IF NOT EXISTS effects (
    -- The job's Temporal workflow id, which is already the deterministic idempotency key derived
    -- from the job and its arguments (`connectors/jobs.py::job_workflow_id`). So a retried run
    -- updates its row rather than forking it, and asking twice for the same effect is one row.
    effect_id     TEXT        PRIMARY KEY,
    connector     TEXT        NOT NULL,
    job           TEXT        NOT NULL,
    -- What was reached, in the operator's words, as the manifest declared it.
    system        TEXT        NOT NULL,
    reversal      TEXT        NOT NULL,
    requested_by  TEXT        NOT NULL DEFAULT '',
    session_id    TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT       NOT NULL DEFAULT '',
    -- Who approved this specific call, for an irreversible effect. Empty for the other two kinds,
    -- which are gated by the plan and by entitlement rather than per call.
    approved_by   TEXT        NOT NULL DEFAULT '',
    state         TEXT        NOT NULL DEFAULT 'attempting',
    -- What the far side called it — a ticket number, a record id. The only handle an operator has
    -- for undoing this by hand, so it is stored even for an effect that failed afterwards.
    external_ref  TEXT        NOT NULL DEFAULT '',
    detail        TEXT        NOT NULL DEFAULT '',
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at    TIMESTAMPTZ,

    CONSTRAINT effects_reversal_known
        CHECK (reversal IN ('idempotent', 'compensating', 'irreversible')),
    CONSTRAINT effects_state_known
        CHECK (state IN ('attempting', 'applied', 'failed', 'compensated')),

    -- An irreversible effect that reached `applied` names who approved it. The constraint rather
    -- than the code, because this is the one field an evidence pack is asked for by name and a
    -- row saying "somebody approved it" is worse in an audit than no row.
    CONSTRAINT effects_irreversible_is_approved
        CHECK (reversal <> 'irreversible' OR state <> 'applied' OR approved_by <> '')
);

-- "What has this system changed lately", and "what did it change in that system".
CREATE INDEX IF NOT EXISTS effects_attempted_idx ON effects (attempted_at DESC);
CREATE INDEX IF NOT EXISTS effects_system_idx ON effects (system, attempted_at DESC);
-- The one an operator runs after an incident: what might have happened and cannot be proved.
CREATE INDEX IF NOT EXISTS effects_unsettled_idx
    ON effects (attempted_at) WHERE state = 'attempting';
