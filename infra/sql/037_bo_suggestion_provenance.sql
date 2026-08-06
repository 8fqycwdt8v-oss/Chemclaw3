-- What a BO suggestion was proposed against, and what run proposed it.
--
-- Two gaps found by the BO deep review (D-2026-08-05-a-ceiling-that-does-not-hold), landing in one
-- migration because they are two columns on one table and splitting them would cost a second
-- deployment for no separable benefit.
--
-- **`problem` — the decision space as it was.** `bo_campaigns.problem` holds the *latest* space,
-- refreshed by every upsert, because a campaign is identified by its problem and a chemist who
-- widens a bound is still working the same optimization. That is right for the campaign and wrong
-- for its history: a suggestion read back after the space widened is then described by a decision
-- space it was not made in, and `read_campaign_thread` hands a later session a proposal whose
-- bounds never applied to it. The candidates and the observations were already snapshotted here
-- for exactly this reason ("a suggestion is only interpretable against the evidence available when
-- it was made"); the space they were drawn from is the third piece of that same statement.
--
-- **`job_id` — the durable run, and the only thing that makes a write idempotent.** Until now the
-- inline tool was the only writer and a retried `record()` simply appended a second identical row:
-- harmless, since the read takes the latest. The durable campaign writes through a Temporal
-- activity, which is retried by design, so the duplicate stops being hypothetical.
--
-- The unique index is on the **run identity**, never on the content. Two genuinely identical asks
-- are two history entries — that is what "the sequence *is* the campaign's history" means in 031 —
-- so deduplicating by candidates-and-observations would erase real history. Deduplicating by the
-- workflow id that produced the row erases only a retry. It is partial (`job_id <> ''`) because the
-- inline path has no run to name, and a NOT NULL DEFAULT '' would otherwise collapse every inline
-- suggestion in a campaign into one.
ALTER TABLE bo_suggestions
    ADD COLUMN IF NOT EXISTS problem JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS job_id  TEXT  NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS bo_suggestions_job_idx
    ON bo_suggestions (campaign_id, job_id) WHERE job_id <> '';
