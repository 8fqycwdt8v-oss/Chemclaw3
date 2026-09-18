-- Every proposed change to what the agent *does*, and what a human decided about it.
--
-- `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` retired the PR-gate over *knowledge* and
-- drew the axis that replaced it: a thing is gated when it changes what the agent does. Knowledge
-- does not, so it lands in the graph and is corrected. A **skill** does — it is injected into the
-- prompt and reshapes every later answer with no citation trail — which is why
-- `agent/skill_backend.SkillsReadOnlyRefusal` refuses every write a turn could attempt, on both
-- tiers.
--
-- That refusal left one thing unanswered: the agent often *is* the party that has just worked out
-- a procedure worth keeping, and until now it could only write the text into an answer and hope
-- somebody copied it. This table is where it puts one instead. A row here is a proposal, never a
-- skill; nothing reads it as judgment; and the only thing that turns one into judgment is a person
-- calling a route.
--
-- **Modelled on `plan_approvals` for the decision and on retired `note_proposals` for the
-- versioning**, because the two halves have different shapes and each has a table that already got
-- its half right.
--
-- From `plan_approvals`: the decision is a record of something a person did at a moment, so rows
-- are appended and never updated, and the read takes the latest.
--
-- From `note_proposals`, including the defect its own successor migration had to fix: the key is
-- the **content**, not the name. Re-proposing byte-identical content is the same proposal, so a
-- rejection in July survives the same text arriving again in August — which is the whole of "an
-- unchanged re-proposal cannot reopen a rejection". A *changed* body is a different proposal and
-- appends a new row, and the earlier one is marked `superseded` rather than left `open`: migration
-- 058 records what happens otherwise, a queue rendering versions nothing would deliver and a
-- decision later applied to both.
--
-- **`kind` rather than one table per kind.** A profile and a skill are different documents with
-- one lifecycle — proposed, decided, superseded — and the surface a person reviews them on asks
-- one question of both. What differs is where an acceptance *lands*, and that is the accepting
-- code's business rather than the record's. Today only `skill` has a destination a route can
-- write: the chemist's own tier. A `profile` acceptance is a record and nothing more, because
-- profiles are git-resident and no route can commit; that is stated in the ADR rather than
-- disguised by a column.
--
-- Not pruned by `chemclaw.durable.retention` — the same reason `plan_approvals` and the retired
-- `note_proposals` are refused there: these are decisions by people about what a system was
-- allowed to become.
CREATE TABLE IF NOT EXISTS behaviour_proposals (
    id             BIGSERIAL   PRIMARY KEY,
    -- What kind of document this is. Constrained rather than free text, because the accepting code
    -- dispatches on it and an unknown kind is a proposal nobody can act on.
    kind           TEXT        NOT NULL,
    name           TEXT        NOT NULL,
    -- The document, verbatim, and a hash of it. The hash is the identity; the body is what makes an
    -- accepted proposal writable without the proposer still being around.
    content_hash   TEXT        NOT NULL,
    content        TEXT        NOT NULL,
    -- Why the proposer thinks this is worth having, in its own words — what a reviewer reads first.
    rationale      TEXT        NOT NULL DEFAULT '',
    -- Who proposed it and in which conversation, from the same ambient carriers `audit_events`
    -- reads, so a proposal joins to the tool call and the words that caused it. `actor` is also
    -- **whose tier an accepted skill is written to**: a proposal is per person, like the tier.
    actor          TEXT        NOT NULL DEFAULT '',
    session_id     TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT        NOT NULL DEFAULT '',
    state          TEXT        NOT NULL DEFAULT 'open',
    proposed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ,
    decided_by     TEXT        NOT NULL DEFAULT '',
    reason         TEXT        NOT NULL DEFAULT '',

    CONSTRAINT behaviour_proposals_kind_known
        CHECK (kind IN ('skill', 'profile')),

    -- `superseded` is not a decision: a newer version of the same name replaced this one in the
    -- queue and no human decided anything about it. Keeping it out of the decided states is what
    -- makes `decided_at` mean what an auditor reads it as.
    CONSTRAINT behaviour_proposals_state_known
        CHECK (state IN ('open', 'accepted', 'rejected', 'superseded')),

    -- A decided row names when. Without this a rejection could be recorded with no timestamp,
    -- which reads in an audit as "someone rejected this at some point" — worse than no row.
    CONSTRAINT behaviour_proposals_decision_is_dated
        CHECK (state NOT IN ('accepted', 'rejected') OR decided_at IS NOT NULL),

    -- **The key, and the reason an unchanged re-proposal cannot reopen a rejection.** Content
    -- identity is per actor because a proposal is per person: two chemists may independently be
    -- offered the same procedure, and one rejecting it must not decide for the other.
    CONSTRAINT behaviour_proposals_version_unique UNIQUE (actor, kind, name, content_hash)
);

-- The three reads. A reviewer wants their own open queue newest-first; a proposer wants to know
-- what became of one proposal; anyone auditing a name wants its whole history in order.
CREATE INDEX IF NOT EXISTS behaviour_proposals_queue_idx
    ON behaviour_proposals (actor, state, proposed_at DESC);
CREATE INDEX IF NOT EXISTS behaviour_proposals_name_idx
    ON behaviour_proposals (actor, kind, name, proposed_at DESC);
