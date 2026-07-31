-- A Bayesian-optimization campaign as an entity, and every suggestion made against it.
--
-- `suggest_next_experiment` is, by its own docstring, "the one-shot human-in-the-loop suggestion"
-- — the path the conversational agent actually uses. It took a decision space and a run history,
-- fitted a surrogate, returned candidates, and **wrote nothing**: not the problem definition, not
-- the observations the agent had assembled out of scattered ELN history, not the candidates, not
-- who asked. The expensive part of an optimization is not the GP fit, which is milliseconds; it is
-- a chemist and an agent jointly framing the problem out of what happened before. That was
-- discarded every turn, so the same framing was rebuilt from scratch on the next question.
--
-- Meanwhile `knowledge/optimization-campaign/` notes exist and come from **DRFP clustering of
-- already-ingested reactions** (`memory/optimization.py`) — a retrospective mechanism with no
-- identity link to any BO run. So the system had a word for a campaign and no object behind it.
--
-- **A campaign is identified by its problem, not minted per call.** `campaign_id` is a hash of the
-- decision space and the objective, so a chemist refining the same optimization across three turns
-- accumulates three suggestions against *one* campaign — which is what makes it an entity rather
-- than a turn — and nobody has to remember to "start" one first. Two people optimizing the same
-- space converge on the same row, which is the behaviour worth having: it is the same campaign.
--
-- Suggestions are append-only. A second ask with more observations is a new proposal, not an edit
-- of the old one; the sequence *is* the campaign's history, and overwriting it would destroy the
-- only record of what was proposed before the latest data arrived.
CREATE TABLE IF NOT EXISTS bo_campaigns (
    campaign_id  TEXT        PRIMARY KEY,
    objective    TEXT        NOT NULL,
    direction    TEXT        NOT NULL,
    -- The full `OptimizationProblem`, descriptors included, so the row reconstructs the space that
    -- was searched without the conversation that framed it.
    problem      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Who first framed it, and when it was last asked about. `last_asked_at` is what separates a
    -- campaign under active work from one abandoned in March.
    opened_by    TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_asked_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT bo_campaigns_direction_known CHECK (direction IN ('minimize', 'maximize'))
);

CREATE TABLE IF NOT EXISTS bo_suggestions (
    id             BIGSERIAL   PRIMARY KEY,
    campaign_id    TEXT        NOT NULL REFERENCES bo_campaigns (campaign_id) ON DELETE CASCADE,
    -- The proposed point(s), and the observations they were derived from. Both, because a
    -- suggestion is only interpretable against the evidence available when it was made: the same
    -- candidate proposed from three runs and from thirty means different things.
    candidates     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    observations   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- The calculation keys the decision space's descriptors came from, so a stale xTB run can be
    -- traced to the suggestions drawn from it — the property D-133 built `calc_refs` for and
    -- D-158 first made real on the QM path.
    calc_refs      TEXT[]      NOT NULL DEFAULT '{}',
    -- Read from the advisory `X-Chemclaw-*` headers (`connectors/caller.py`), which is what these
    -- headers were sent for: joining a connector's own records to core's audit trail.
    actor          TEXT        NOT NULL DEFAULT '',
    session_id     TEXT        NOT NULL DEFAULT '',
    correlation_id TEXT        NOT NULL DEFAULT '',
    proposed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The campaign's own history, newest first: "what have we proposed here, and on what evidence".
CREATE INDEX IF NOT EXISTS bo_suggestions_campaign_idx
    ON bo_suggestions (campaign_id, id DESC);
-- The join to a conversation, which is the whole reason the caller is recorded.
CREATE INDEX IF NOT EXISTS bo_suggestions_session_idx
    ON bo_suggestions (session_id) WHERE session_id <> '';
