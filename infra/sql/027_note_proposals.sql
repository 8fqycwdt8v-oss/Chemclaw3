-- RETIRED. The PR-gate's record of every agent-authored note ever proposed and what a human
-- decided about it.
--
-- `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` ended the gate: knowledge is written
-- straight into the graph and corrected rather than pre-approved, so nothing proposes a note and
-- nothing decides one. No row is written here any more.
--
-- **The table stays and is not dropped**, for two reasons rather than one. The schema is
-- forward-only — a migration may not drop a table or a column
-- (`tests/test_migrations_are_additive.py`) — and a deployment that ran the gate holds real
-- sign-offs by real people, which `agent/leaver.py::_RETAINED` still has to find on an erasure
-- request. `durable/retention.py` no longer refuses it: there is no live record to protect.
--
-- The statements below are unchanged; re-running this file on an existing database is a no-op.

-- Every agent-authored note ever submitted to the PR-gate, and what a human decided about it.
--
-- The PR-gate is named in `CLAUDE.md`, `ARCHITECTURE.md`, `SECURITY.md` and D-005 as the line the
-- whole system is justified by: the agent proposes, a human reviews before it becomes knowledge. In code it ended at a branch
-- push. `chemclaw.kg.git_submitter.submit` returns `note/<id>` and nothing calls a git platform,
-- so there was no way to *list* what is awaiting review, no way for the chemist who proposed a
-- note to see what became of it, and — the part that matters for an audit — **no record at all of
-- a proposal that was rejected**, because a rejection is a deleted branch. The one durable trace
-- was `job_records.note_id`, and only for connector jobs.
--
-- **Append-per-version, not one row per note.** A decision is evidence and must not be overwritten
-- by the next submission: "this was rejected in July" has to survive the note being re-proposed in
-- August. So the key is the *content*, not the note: re-proposing byte-identical content touches
-- the existing row (matching the submitter's own idempotent no-op, which pushes nothing when there
-- is no diff), while a changed body appends a new row and leaves the earlier decision standing.
-- `content` is kept verbatim for the same reason the `failed` state exists at all: a submission
-- that never reached git is only replayable if the bytes it would have written are still here.
--
-- Not pruned by `chemclaw.durable.retention` — this is a compliance record of human decisions,
-- which is the same reason `audit_events` and `job_records` are refused there. Applied by
-- `make db-migrate`.
CREATE TABLE IF NOT EXISTS note_proposals (
    id             BIGSERIAL   PRIMARY KEY,
    note_id        TEXT        NOT NULL,
    note_type      TEXT        NOT NULL,
    -- The rendered note, and a hash of it. The hash is what makes a re-proposal idempotent; the
    -- body is what makes a `failed` submission replayable instead of merely counted.
    content_hash   TEXT        NOT NULL,
    content        TEXT        NOT NULL,
    branch         TEXT        NOT NULL,
    -- The submitter's own reference (today the branch name; a PR URL once an adapter exists).
    reference      TEXT        NOT NULL DEFAULT '',
    -- Who proposed it and in which conversation, read from the same ambient carriers
    -- `audit_events` reads, so a proposal joins to the tool call and the words that caused it.
    actor          TEXT        NOT NULL DEFAULT '',
    session_id     TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT        NOT NULL DEFAULT '',
    state          TEXT        NOT NULL DEFAULT 'open',
    submitted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    decided_by     TEXT        NOT NULL DEFAULT '',
    reason         TEXT        NOT NULL DEFAULT '',

    -- `failed` is not a decision: it means the submission never reached git, so there is nothing
    -- for a human to have decided about. The other three are the reviewer's answers.
    CONSTRAINT note_proposals_state_known
        CHECK (state IN ('open', 'merged', 'rejected', 'failed')),

    -- A decided row names when. Without this a rejection could be recorded with no timestamp,
    -- which reads in an audit as "someone rejected this at some point" — worse than no row.
    CONSTRAINT note_proposals_decision_is_dated
        CHECK (state NOT IN ('merged', 'rejected') OR decided_at IS NOT NULL),

    CONSTRAINT note_proposals_version_unique UNIQUE (note_id, content_hash)
);

-- The three reads. A reviewer wants the open queue newest-first; a proposer wants their own
-- submissions; anyone auditing one note wants its whole history in order.
CREATE INDEX IF NOT EXISTS note_proposals_state_idx ON note_proposals (state, submitted_at DESC);
CREATE INDEX IF NOT EXISTS note_proposals_actor_idx ON note_proposals (actor, submitted_at DESC);
CREATE INDEX IF NOT EXISTS note_proposals_note_idx ON note_proposals (note_id, submitted_at DESC);
