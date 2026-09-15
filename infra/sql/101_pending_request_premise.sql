-- What a durable question rests on, so an answer cannot be applied to a premise that has since
-- gone (`D-2026-09-15-an-answer-days-later-is-answered-against-a-corpus-that-moved`).
--
-- A wait holds a question open for up to `awaiting_max_days` (90), and the BO case deliberately
-- waits a week for plates. Over that span the corpus moves: `memory/supersede.retire_note` closes a
-- note a synthesis replaced, and `memory/failure.close_refuted_note` closes one a reported failure
-- refuted. Nothing asked whether either had happened before the answer released the workflow.
--
-- **The ids, not a fingerprint of them.** A fingerprint would answer "did this change since the
-- ask", which needs two readings taken on two pods — and the knowledge checkout is a sidecar-
-- refreshed clone, so two API replicas are routinely minutes apart and a sync landing mid-wait
-- would read as a change that never happened. Storing the ids asks the narrower question ("does it
-- still hold") on whichever corpus the answering pod has, and `agent/pending_tools.py` refusing to
-- *open* a wait on an already-broken premise is what makes that narrower question sufficient: every
-- wait that exists had a whole premise at ask time.
--
-- `TEXT[]` rather than JSONB: this is a list of slugs with no structure, `= ANY(...)` is the only
-- predicate anything would ever want, and `kg/note.py::_SLUG` already constrains what may be in it.
ALTER TABLE pending_requests
    ADD COLUMN IF NOT EXISTS premise_note_ids TEXT[] NOT NULL DEFAULT '{}';
