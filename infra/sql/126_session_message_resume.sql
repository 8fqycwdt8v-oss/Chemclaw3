-- A turn whose pod was killed between two steps is resumed from its checkpoint by its sender's next
-- attach (D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted).
--
-- Both columns are set only on the question a turn opens, as `turn_status` is (118), and read as
-- "nothing recorded" when NULL, so the column is additive and the previous image ignores it.
--
-- `dry_run` is what the turn was started with. A resumed turn runs with the same flag: the request
-- that carried it is gone with the pod, and a dry run resumed as a live one would be a turn the
-- chemist never asked for. NULL reads as false.
--
-- `resumed_at` is the instant a successor took the dead turn over. It is the one-resume bound (a
-- turn that kills its pod twice is not resumed a third time) and, in the same transaction as the
-- successor's `session_turns` claim, the compare-and-set that makes two replicas attaching at once
-- resume it once. The claim that covers a question is the one taken at or before `resumed_at`, or
-- at or before `created_at` when the turn was never resumed (`agent/session_store.py`).
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS dry_run BOOLEAN;
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS resumed_at TIMESTAMPTZ;
