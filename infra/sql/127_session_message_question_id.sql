-- The id a turn's question has in the checkpointed thread, kept with the question's row
-- (D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted; completes 126).
--
-- The server mints it. A correlation id is a header any client may send, and LangGraph replaces a
-- message that repeats an id, so naming the question by it let one participant overwrite another's
-- question in the thread the model reads. A resume finds the question in the thread by this column.
-- NULL on a turn the previous image began, which is therefore never resumed; the column is
-- additive and the previous image ignores it.
ALTER TABLE session_messages ADD COLUMN IF NOT EXISTS question_id TEXT;
