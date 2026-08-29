-- The work this system is waiting on a person or an instrument for (F2).
--
-- Nothing in this tree could wait. `grep -rn "workflow.signal\|wait_condition\|workflow.update"
-- src/` returned **zero hits**: Temporal was here and used only for compute that starts and
-- finishes. Every human decision was modelled as refuse-and-retry — the plan gate refuses, the turn
-- ends, a human clicks, a later turn proceeds — which works inside a conversation and cannot
-- represent a process that outlives one. So a screening campaign could not suspend for the week its
-- plates take, and a manager's whole working world (a gate review, a CRO deliverable, a stability
-- pull) had no shape here at all.
--
-- **Why a table beside the workflow, when the workflow already holds the state.** Temporal knows
-- every waiting run and can answer "is this one done", but not "what is waiting on *me*": listing
-- requires a visibility query per user against a broker this deployment self-hosts, and the answer
-- would carry none of the subject, the requester or the reason. The workflow remains the authority
-- on *whether* the wait is still open — this row is a projection of it, written by the workflow's
-- own activities, exactly as `job_records` projects a finished job.
--
-- `request_id` is the Temporal workflow id, which is derived deterministically from what is being
-- asked (`durable/awaiting.py::request_id_for`). So asking twice for the same thing is one wait,
-- and a retry of the opening activity updates rather than forks.
CREATE TABLE IF NOT EXISTS pending_requests (
    request_id     TEXT        PRIMARY KEY,
    -- What class of answer is expected. A bounded vocabulary so a surface can group an inbox
    -- without reading the subject line.
    kind           TEXT        NOT NULL,
    -- What is being asked, in the requester's words, and why. Free text a caller supplied: it is
    -- shown back to the person being asked, which is the whole point, and it is therefore never
    -- returned by an aggregate (`chemclaw.operations` returns counts and bounded vocabularies).
    subject        TEXT        NOT NULL,
    rationale      TEXT        NOT NULL DEFAULT '',
    -- Who is expected to answer: an actor id, or an entitlement, or '' for anyone entitled. This
    -- is *advisory routing*, not the control — the API route is what enforces who may answer, for
    -- the reason `D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned` gives about
    -- anything lifted out of an unsigned workflow payload.
    asked_of       TEXT        NOT NULL DEFAULT '',
    requested_by   TEXT        NOT NULL DEFAULT '',
    session_id     TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT        NOT NULL DEFAULT '',
    state          TEXT        NOT NULL DEFAULT 'waiting',
    -- When the wait gives up. Bound at open time from the request, so a deadline is a property of
    -- the ask rather than of whichever worker happens to be running it.
    due_at         TIMESTAMPTZ NOT NULL,
    reminders      INTEGER     NOT NULL DEFAULT 0,
    reminded_at    TIMESTAMPTZ,
    answered_at    TIMESTAMPTZ,
    answered_by    TEXT        NOT NULL DEFAULT '',
    -- The answer itself, opaque to this table: a measurement set for a campaign, a decision for an
    -- approval. Typed by whoever asked, validated there.
    answer         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pending_requests_state_known
        CHECK (state IN ('waiting', 'answered', 'expired', 'cancelled')),

    -- An answered row names when and by whom. Without this an answer could be recorded with no
    -- timestamp and no actor, which reads in an audit as "somebody answered at some point" —
    -- worse than no row. The same rule `note_proposals` applies to a decision.
    CONSTRAINT pending_requests_answer_is_attributed
        CHECK (state <> 'answered' OR (answered_at IS NOT NULL AND answered_by <> ''))
);

-- The inbox query: what is still open, soonest deadline first.
CREATE INDEX IF NOT EXISTS pending_requests_open_idx
    ON pending_requests (due_at) WHERE state = 'waiting';

-- "What is waiting on me", and "what did I ask for".
CREATE INDEX IF NOT EXISTS pending_requests_asked_of_idx
    ON pending_requests (asked_of, due_at) WHERE state = 'waiting';
CREATE INDEX IF NOT EXISTS pending_requests_requester_idx
    ON pending_requests (requested_by, created_at DESC);
