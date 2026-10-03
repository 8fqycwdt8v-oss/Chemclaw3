-- A chemist's message is written ahead of its turn, and the turn's ending is a column on it
-- (D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so).
--
-- Measured on the kind cluster (K5 §1): a front-door pod killed mid-turn left the chemist's message
-- in the LangGraph checkpoint and absent from `session_messages`, because the transcript was written
-- once, after the answer. The next turn's model saw a question the chemist's transcript did not show,
-- the reattach answered a bare 404, and no `turn_costs` row recorded how the turn ended.
--
-- `turn_status` is set only on the question a turn opens: `running` when the turn starts, settled
-- by the turn's own process to `done`, `failed` or `stopped`, or to `interrupted` by whichever
-- process next touches the session once the turn's `session_turns` lease has lapsed with no live
-- owner. `NULL` on every other row and on every row written before this column, which readers take
-- as "nothing recorded" — so the column is additive and the previous image ignores it.
--
-- No CHECK constraint: the vocabulary is `agent/session_store.TurnStatus`, and its one reader maps
-- an unknown spelling to `NULL` (`turn_status_of`). A constraint added here would have to be
-- dropped and re-added by every future widening, which `README.md`'s replay notes record as a hazard.
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS turn_status TEXT;

-- What `mark_interrupted` and the transcript read probe on every touch of a session: is there a
-- question still running? Partial, so it holds only the handful of rows in flight at any moment and
-- costs an ordinary append nothing.
CREATE INDEX IF NOT EXISTS session_messages_running_turn_idx
    ON session_messages (session_id) WHERE turn_status = 'running';
