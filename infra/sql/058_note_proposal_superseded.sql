-- RETIRED with migration 027 (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). The
-- `superseded` state stays in the CHECK for the rows that already hold it; nothing writes one.

-- A re-proposed note closes its own previous open version.
--
-- The branch is per-note (`note/<id>`) while the record is per-version (`(note_id,
-- content_hash)`), so re-proposing a *changed* note force-pushed the one branch and left the
-- earlier version's row `open`: the review queue rendered bytes that existed on no branch, and
-- the merge webhook's open-rows predicate then marked *both* rows merged — the compliance table
-- asserting a human merged content that was never merged. `superseded` is the state that makes
-- the collapse honest: not a decision (no human decided anything about the old version), not a
-- failure, just "a newer version of this note replaced it in the queue".
ALTER TABLE note_proposals DROP CONSTRAINT note_proposals_state_known;
ALTER TABLE note_proposals
    ADD CONSTRAINT note_proposals_state_known
        CHECK (state IN ('open', 'merged', 'rejected', 'failed', 'superseded'));
