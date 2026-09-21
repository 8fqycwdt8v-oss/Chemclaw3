-- What a designed arm actually produced: the loop `experiment_protocols` (073) left open.
--
-- **A design reached `executed` and nothing attached the outcome.** 073 built the prescriptive tier
-- — what to run, as a revisable document — and 052 holds the descriptive one, what a chemist did.
-- Neither joins them. A plate was laid out here, exported as a run sheet, run, and its numbers came
-- back into a spreadsheet: so the round trip `skills/hte-campaign-design` promises in its own
-- closing section ("the arms that survive become the observations `suggest_next_experiment` fits a
-- surrogate to") was a person retyping a table, and the `DEFERRED.md` row on mining the
-- agent-to-human protocol diff had no corpus, because nothing could tell which designs had ever
-- been run at all.
--
-- **Why a third table rather than a column on either tier, which is the question the backlog row
-- posed.** A `design_id` column on `reaction_records` was the cheap answer and is wrong twice: that
-- table is fed by `ingest/eln`, whose exports carry no design id, so the column would be NULL on
-- every ingested row and populated only by hand; and `D-2026-08-28-a-protocol-is-prescriptive-and-
-- a-record-is-not` deliberately reuses *none* of that tier's shapes, because a record of what was
-- done and an instruction to do it are different facts. Folding outcomes into the design document
-- is wrong for the mirror reason: a revision is what a human *changed*, append-only precisely so
-- the correction survives, and an outcome is not a correction.
--
-- So the join is its own table, keyed by the two things the prescriptive tier already names — the
-- design and the arm. That key is what makes an externally-run plate attachable: a chemist who ran
-- the run sheet in another lab has a `design_id` and an `arm_id` and nothing else, and this table
-- asks for nothing else. `reaction_id` is optional for exactly that reason — it links the outcome
-- to its ELN transcription when one exists, and stays NULL when the run lives only on paper.
--
-- **Append-only, like the revisions beside it, and for a related reason.** A re-measured well is a
-- second observation rather than a correction of the first: an assay repeated a week later on a
-- degraded sample is data about the sample, and overwriting would delete the evidence that the two
-- disagree. Readers take the latest per (design, arm, outcome) and the disagreement stays visible.

CREATE TABLE IF NOT EXISTS experiment_arm_results (
    -- Surrogate key, because the natural one is (design, revision, arm, outcome, measured_at) and
    -- an append-only table must admit the same well twice at the same instant without a race.
    result_id       BIGSERIAL   PRIMARY KEY,
    design_id       TEXT        NOT NULL
                                REFERENCES experiment_protocols (design_id) ON DELETE CASCADE,
    -- The revision whose arms these outcomes are against. **Not the head**: a plate is run from the
    -- revision a chemist printed, and a later edit must not silently re-point old numbers at arms
    -- that no longer mean the same thing.
    revision        INTEGER     NOT NULL,
    -- The `arm_id` from that revision's design. Deliberately not checked against it in SQL — the
    -- arms live inside a JSONB document, and a constraint that cannot see them would be a control
    -- whose condition never occurs. `protocols.results` checks it against the stored design, where
    -- the refusal can name the arms that do exist.
    arm_id          TEXT        NOT NULL,
    -- What was measured, in the vocabulary the design's own `analytics.measures` uses, so an
    -- objective the plate was run for can be matched to the number that answers it.
    outcome         TEXT        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    -- The unit as `core/units` spells it. Stored rather than assumed, because a yield in percent
    -- and an assay in mg/mL are both numbers and only one of them is comparable to a limit.
    unit            TEXT        NOT NULL DEFAULT '',
    -- The ELN transcription of this run, when there is one. NULL is the ordinary case for a plate
    -- run outside this system, and is what keeps such a plate attachable at all.
    reaction_id     TEXT,
    -- When the measurement was taken, which is not when it was recorded here.
    measured_at     TIMESTAMPTZ,
    -- Who attached it, on the same terms as `experiment_protocol_revisions.author`.
    author_kind     TEXT        NOT NULL DEFAULT 'human',
    author          TEXT        NOT NULL DEFAULT '',
    note            TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT experiment_arm_results_author_known
        CHECK (author_kind IN ('agent', 'human')),
    CONSTRAINT experiment_arm_results_outcome_present
        CHECK (outcome <> ''),
    CONSTRAINT experiment_arm_results_arm_present
        CHECK (arm_id <> '')
);

-- The read every caller makes: every outcome for one design, newest first, so the latest per
-- (arm, outcome) is the first row seen without a window function.
CREATE INDEX IF NOT EXISTS experiment_arm_results_design_idx
    ON experiment_arm_results (design_id, revision, created_at DESC);

-- The join back to the ELN, for "which designed arms have a transcription".
CREATE INDEX IF NOT EXISTS experiment_arm_results_reaction_idx
    ON experiment_arm_results (reaction_id)
    WHERE reaction_id IS NOT NULL;
