-- A workflow the agent composed at run time, so a procedure it works out once can be re-run
-- (D-2026-09-15-an-agent-authored-workflow-is-read-only-by-construction).
--
-- Until now a template was a git-committed YAML file and nothing else, and that was load-bearing
-- rather than incidental: `D-2026-08-12-a-template-is-the-plan-so-the-step-is-read-only` exempts a
-- template's `agent` step from the plan gate *because* the file was authored by a person and
-- "nothing at run time can produce one". An agent that could produce one, naming a write in its
-- steps or in `write_tools:`, would be granting itself exactly the ungated write path that
-- exemption assumes cannot exist.
--
-- What makes this table safe is therefore not the table. It is that an agent-authored workflow may
-- name **no side-effecting tool and no `write_tools`** — checked when it is stored and checked
-- again when it is run, by `templates/composed.py::authored_problems`, and no approval lifts
-- either. The exemption is about writes; a document that cannot contain one does not reach it. A
-- hand-written template in `data/templates/` is unaffected and keeps every capability it had.
--
-- **A durable `job` step is the one thing on the other side of that line**, and migration 103 is
-- where it is decided: a job's name and arguments are in the document a person read, so a person
-- can approve them, and `approved_fingerprint` records which version they approved. This header
-- said "no durable job" in the present tense for as long as that was true and one migration longer
-- — read 103 next, not this paragraph, for what a job step costs.
--
-- `document` is the whole `Template` as validated JSON, pinned the way a run pins its template:
-- what ran is what was stored, and a later edit is a new row rather than a rewrite of history that
-- an in-flight run would disagree with.
--
-- Keyed by `(owner, name)` rather than by name alone. A composed workflow is one chemist's working
-- procedure, not a deployment's catalogue — two people are entitled to their own "degradant
-- triage" without one silently replacing the other's, and `run_composed_workflow` resolves against
-- the caller's own rows so a name can never reach somebody else's steps.

CREATE TABLE IF NOT EXISTS composed_workflows (
    owner       TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    document    JSONB       NOT NULL,
    -- What the author said it is for, kept out of `document` so a listing needs no JSON read.
    summary     TEXT        NOT NULL DEFAULT '',
    -- The session and turn it was composed in, so a workflow that later looks wrong can be traced
    -- back to the conversation that produced it — the same join every audit row already supports.
    -- `session_id` is selected by the store and shown on the approval screen, so an approver
    -- looking at steps they did not write can find the exchange that asked for them.
    -- `correlation_id` is the turn, and is deliberately an operator's column: nothing in `src/`
    -- selects it, because what a reader wants of one turn is the `audit_events` rows that share
    -- the id, which is a query rather than a field on this row.
    session_id      TEXT    NOT NULL DEFAULT '',
    correlation_id  TEXT    NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, name)
);

-- The listing this table is read by: one owner's workflows, most recently changed first.
CREATE INDEX IF NOT EXISTS composed_workflows_owner_idx
    ON composed_workflows (owner, updated_at DESC);
