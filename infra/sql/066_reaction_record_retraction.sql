-- A source-reported withdrawal, so a retracted run stops answering as current evidence
-- (D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry).
--
-- An ELN entry withdrawn upstream simply disappears from the export, and a cursor-based sync never
-- sees an absence: measured, the run after a withdrawal reports `ingested=[] rejected=[]` and the
-- row keeps answering `is_current(today) = true`, so `FingerprintReactionRetriever` serves a
-- withdrawn run as current evidence with nothing in the summary, the log line or the counters
-- saying so.
--
-- **`retracted_at`, not `valid_to`.** `052`'s record deliberately has no validity window, and
-- `ReactionRecord.is_current` states the reason: a *result* does not expire on its own, it is
-- superseded, which is a claim a human makes in a note. A source withdrawing an entry is neither —
-- it is the originating system saying the entry should not have been published. Different fact,
-- different name, so the two can never be confused for one another in a query or in review.
--
-- **The row is never deleted.** `durable/retention.py` already refuses to prune this table because
-- a row is the only readable form of an ELN run; a retraction makes that *more* true, not less —
-- "what did we think we knew, and when did we stop" is unanswerable once the row is gone. So
-- `read()` keeps serving a retracted row and `eligible()` stops, which is what makes a retraction
-- readable as of an earlier date.
--
-- Nullable with no default and no backfill: `NULL` is "not retracted", and it is the honest value
-- for every row already stored. Nothing in an existing row says whether its entry was withdrawn,
-- and a source that can report retractions will report the ones inside its own window on the next
-- sync (D-2026-08-04-the-schema-only-goes-forward).
ALTER TABLE reaction_records ADD COLUMN IF NOT EXISTS retracted_at TIMESTAMPTZ;

COMMENT ON COLUMN reaction_records.retracted_at IS
    'When the source reported this entry withdrawn; NULL means not retracted. Set only from a '
    'retraction the source reports — never inferred from an entry''s absence from an export, '
    'because an ELN fetch is a delta and "not seen this run" is the normal state of every entry '
    'ever ingested. The row is kept and stays readable; only current-evidence queries drop it.';

-- The eligibility filter already reads `project`/`performed_at` together (`052`'s
-- `reaction_records_filter_idx`) over the id set a fingerprint search just returned. A retraction
-- is expected to be rare, so this is a partial index on the retracted rows alone — small, and it
-- is the direction the sweep asks about ("which of these are already retracted?"), while the
-- eligibility query's `retracted_at IS NULL` is satisfied by the far larger complement that the
-- PK lookup has already narrowed.
CREATE INDEX IF NOT EXISTS reaction_records_retracted_idx
    ON reaction_records (ingest_source, reaction_id)
    WHERE retracted_at IS NOT NULL;
