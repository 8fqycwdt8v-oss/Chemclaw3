-- Experiment protocols: the prescriptive tier.
--
-- Every reaction shape this system already had is **descriptive**. `reaction_records` (052) is a
-- transcription of what a chemist did, and the ORD schema it borrows says so in as many words: a
-- record "should describe what was actually done in the lab, and not an idealized protocol or
-- instruction set". Nothing recorded what to *do*, so a generated protocol lived in one transcript,
-- had no id to reopen, no revision, and no way to observe the edit the chemist made to it.
--
-- **Rows and not PR-gated notes**, for `052`'s argument arriving from the other side. That table is
-- ungated because a transcription hands a reviewer nothing to decide; a draft is ungated because
-- the decision it needs is *running it*, which happens in a laboratory rather than in a review
-- queue. What a human asserts about a design once it has run is still a playbook or an
-- experiment-proposal note in `knowledge/`, gated as it always was, citing the design.
--
-- **The revision table is append-only and that is the whole design.** A chemist alters almost every
-- first draft, and that alteration is the most informative thing this system can observe about its
-- own suggestions — a labelled correction from the person with the most context. Updating a
-- document in place would keep the protocol and throw away the correction.

CREATE TABLE IF NOT EXISTS experiment_protocols (
    -- `design-<hash>` over the structured ask, so the same request restructured in one session
    -- reaches the same design instead of forking one (`protocols.models.design_id_for`).
    design_id       TEXT        PRIMARY KEY,
    title           TEXT        NOT NULL,
    -- `single` is one arm and no factors; `screen` is a fixed array run as a batch; `campaign`
    -- expects to be re-asked once results arrive. One column rather than a boolean because the
    -- checks differ: a campaign's first round may legitimately not cover its factor space.
    mode            TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'draft',
    project         TEXT        NOT NULL DEFAULT '',
    -- Who opened it. Retained rather than erased on offboarding, for the reason
    -- `bo_campaigns.opened_by` is: a design is a shared scientific artifact and the person who
    -- opened it is part of its provenance. `agent/leaver.py` states that position explicitly.
    opened_by       TEXT        NOT NULL DEFAULT '',
    session_id      TEXT        NOT NULL DEFAULT '',
    correlation_id  TEXT        NOT NULL DEFAULT '',
    -- Denormalised from the revision table so a listing is one query. Recomputed on every append;
    -- never the source of truth, which is the revision rows themselves.
    head_revision   INTEGER     NOT NULL DEFAULT 0,
    arm_count       INTEGER     NOT NULL DEFAULT 0,
    blocker_count   INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT experiment_protocols_mode_known
        CHECK (mode IN ('single', 'screen', 'campaign')),
    CONSTRAINT experiment_protocols_status_known
        CHECK (status IN ('requested', 'draft', 'approved', 'executed', 'abandoned'))
);

-- The two listings the front door serves: "what is open" and "what is this project working on".
CREATE INDEX IF NOT EXISTS experiment_protocols_status_idx
    ON experiment_protocols (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS experiment_protocols_project_idx
    ON experiment_protocols (project, updated_at DESC);
-- Partial, because most designs are opened outside a session that is still around to ask.
CREATE INDEX IF NOT EXISTS experiment_protocols_session_idx
    ON experiment_protocols (session_id)
    WHERE session_id <> '';

CREATE TABLE IF NOT EXISTS experiment_protocol_revisions (
    design_id       TEXT        NOT NULL
                                REFERENCES experiment_protocols (design_id) ON DELETE CASCADE,
    -- 1-based and gapless. The primary key is what makes two concurrent appends that both read
    -- the same head resolve to one winner even if they race past the application's own check.
    revision        INTEGER     NOT NULL,
    -- `request` holds only the structured ask — the intake a chemist corrects before the expensive
    -- work starts; `protocol` holds a whole design. Two kinds in one table because they are the
    -- same document growing, and a reader wants one history rather than two.
    kind            TEXT        NOT NULL,
    -- The reason this table exists in this shape: an agent draft and a human edit are the two
    -- sides of the signal, and telling them apart afterwards has to be a column rather than a guess
    -- from the actor string.
    author_kind     TEXT        NOT NULL,
    author          TEXT        NOT NULL DEFAULT '',
    -- 0 on the first revision; the head it was derived from on every later one. Compared before
    -- the insert, so a concurrent edit is a refusal the writer sees rather than a silent overwrite.
    parent_revision INTEGER     NOT NULL DEFAULT 0,
    change_note     TEXT        NOT NULL DEFAULT '',
    document        JSONB       NOT NULL,
    -- What `protocols.checks` said about *this* revision. Stored beside the document rather than
    -- recomputed on read, because the checks are a statement about the design as it stood — a
    -- later code change to a check must not silently rewrite the verdict a chemist acted on.
    checks          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (design_id, revision),
    CONSTRAINT experiment_protocol_revisions_kind_known
        CHECK (kind IN ('request', 'protocol')),
    CONSTRAINT experiment_protocol_revisions_author_known
        CHECK (author_kind IN ('agent', 'human'))
);

-- History is read oldest-first and the head is read newest-first; one index covers both.
CREATE INDEX IF NOT EXISTS experiment_protocol_revisions_history_idx
    ON experiment_protocol_revisions (design_id, revision DESC);
