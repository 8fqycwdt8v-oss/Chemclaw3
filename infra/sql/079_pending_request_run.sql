-- The Temporal run a projected wait belongs to, so a re-ask can reopen its row.
--
-- `pending_requests.request_id` is the *workflow* id, which `request_id_for` derives from
-- (kind, subject, asked_of) so that asking the same question twice joins one wait rather than
-- opening two. `request_external_input` then sets `WorkflowIDReusePolicy.ALLOW_DUPLICATE`
-- deliberately, because "expiry is an ordinary ending — asking again after a deadline passed is a
-- new ask".
--
-- The projection did not know that. `076`'s upsert guards `WHERE state = 'waiting'`, which is right
-- for its stated case (a retry of the opening activity must update rather than fork) and wrong for
-- the case the launcher enables: after a wait expired, the same question asked again wrote nothing.
-- The row stayed `expired` with the *previous* cycle's deadline, so `open_requests` never listed it,
-- `record_reminder` no-opped, and the answer route read the stale state and returned
-- 409 "already expired" — to everyone, forever, about a wait that was genuinely running.
--
-- The run id separates the two cases exactly: same run means a retry (update in place, keep the
-- state), different run means a new ask (reopen, clearing the previous cycle's answer fields).
ALTER TABLE pending_requests
    ADD COLUMN IF NOT EXISTS run_id TEXT NOT NULL DEFAULT '';
