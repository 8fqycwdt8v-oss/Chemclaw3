-- The ungated observations tier (D-161).
--
-- Knowledge has had exactly one tier and one gate: a note is agent-proposed, a human merges it,
-- and only then is it readable. That is right for anything asserted as fact, and it is the reason
-- there is no proactive cross-project learning loop — every candidate learning would cost a human
-- a review, and most candidates are not worth one.
--
-- An observation is explicitly *not* truth: "these two projects have both failed this coupling"
-- is worth noticing and is not worth a PR. The human gate does not disappear, it moves — from
-- every observation to the few worth promoting into a playbook note, which still goes through the
-- same PR-gate as everything else.
--
-- **Postgres, not Git.** Git's value is human review, diff and audit. With no review it buys PR
-- noise and repo churn and returns nothing, while a table gives cheap upsert-accumulation of
-- support, TTL eviction, and no branch-per-note explosion. This *preserves* "git is the source of
-- truth" precisely because observations are not truth.
CREATE TABLE IF NOT EXISTS observations (
    -- `observation-<hash of scope+statement>`, so re-mining the same finding accumulates support
    -- onto one row instead of minting a second, near-identical one every night.
    id                 TEXT        PRIMARY KEY,
    statement          TEXT        NOT NULL,
    -- What the statement is about: a transformation class, a chemotype, a process step. Free text
    -- rather than an enum — the miners choose it, and an enum here would be a schema migration
    -- every time a new kind of thing is worth observing.
    scope              TEXT        NOT NULL,
    -- The **merged** notes this rests on. Support is `cardinality(evidence_note_ids)`, never a
    -- separate counter: a counter can be incremented by something that is not a merged note, and
    -- the whole safety of this tier is that it cannot inflate its own support.
    evidence_note_ids  TEXT[]      NOT NULL DEFAULT '{}',
    projects_seen      TEXT[]      NOT NULL DEFAULT '{}',
    origin             TEXT        NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'open',
    first_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT observations_status_known CHECK (status IN ('open', 'promoted', 'retired')),
    CONSTRAINT observations_origin_known CHECK (origin IN ('corpus-mining', 'interaction')),

    -- The anti-feedback rule, as a constraint rather than a guideline (D-161).
    --
    -- Without it the failure mode is not hypothetical and is not obvious from the outside: the
    -- agent writes an observation, a later run retrieves its own observation, counts it as
    -- corroboration, and inflates past the promotion threshold into a PR — a self-confirming loop
    -- wearing the costume of cross-project evidence. Support may only ever be a note a human
    -- merged, so an observation id can never appear in this column at all.
    CONSTRAINT observations_evidence_is_merged_notes
        CHECK (array_to_string(evidence_note_ids, ' ') NOT LIKE '%observation-%')
);

-- The two reads: the retrieval bucket wants open observations newest-first, and the promotion
-- sweep wants the open ones with enough support. Both filter on status, so it leads.
CREATE INDEX IF NOT EXISTS observations_status_idx ON observations (status, last_seen DESC);
