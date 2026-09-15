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
-- name **no side-effecting tool, no durable job and no `write_tools`** — checked when it is stored
-- and checked again when it is run, by `templates/composed.py::authored_problems`. The exemption
-- is about writes; a document that cannot contain one does not reach it. A hand-written template
-- in `data/templates/` is unaffected and keeps every capability it had.
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
    session_id      TEXT    NOT NULL DEFAULT '',
    correlation_id  TEXT    NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, name)
);

-- The listing this table is read by: one owner's workflows, most recently changed first.
CREATE INDEX IF NOT EXISTS composed_workflows_owner_idx
    ON composed_workflows (owner, updated_at DESC);
