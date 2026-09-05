-- What a turn looked at, what it cited, and what it wrote back — the dimensions this row lacked.
--
-- `turn_costs` has recorded a turn's spend since 033 and how it *ended* since 060. What neither
-- covers is whether the turn consulted the knowledge record at all, whether its answer cited what
-- it found, and whether anything was written back. Two separate reviews of this system's knowledge
-- loop had to answer those with bespoke scripts, because no metric series and no table held them —
-- and the answers are not recoverable after the fact: the event stream is gone once the turn ends,
-- and `session_messages` holds the prose rather than which tool ran.
--
-- The one that motivated this: `retrieval_calls = 0` on a turn that made a claim about this
-- programme's own chemistry. The system prompt gained an obligation to search before answering
-- (`D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker`), and this is the only way to
-- find out whether that obligation moved anything. A prompt rule nobody can measure is a hope.
--
-- `answer_confidence`, `review_required` and `notes_cited` are computed by `score_answer` on
-- **every** production turn already. They were streamed to the client and discarded; `api/schemas`
-- records that they are not written to `session_messages` either. This is the first store to keep
-- them.
--
-- Additive and defaulted throughout, per `infra/sql/README.md`: every existing row keeps its
-- meaning and the previous image can still write, because nothing here is NOT NULL without a
-- default.

-- Tool calls this turn made against the knowledge record (`authz.KNOWLEDGE_READ_TOOLS`) and against
-- the write surface (`authz.side_effecting_tools()`). Zero is a real, common and interesting value
-- on both: a turn that answered without looking, and a turn that learned something and kept it to
-- itself.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS retrieval_calls INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS capture_calls   INTEGER NOT NULL DEFAULT 0;

-- The verifier's aggregate citation-faithfulness score in [0, 1], or NULL when it did not run.
--
-- **Nullable on purpose, and it must not be defaulted to 0.** `verifier_enabled` can be off, and a
-- turn routed to review by the deterministic answer-shape gate carries `review_required = true`
-- with no score at all. Storing a 0 there would read as "graded, and graded terrible" for a turn
-- that was never graded — the ambiguous zero `D-2026-08-03-a-metric-must-declare-what-it-can-see`
-- is about, in a column.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS answer_confidence DOUBLE PRECISION;
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS review_required BOOLEAN NOT NULL DEFAULT FALSE;

-- How many `[[note ids]]` the answer cited. The join from "we retrieved" to "we used it": a turn
-- with `retrieval_calls > 0` and `notes_cited = 0` searched the record and then answered from
-- somewhere else, which is a different failure from never having looked.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS notes_cited INTEGER NOT NULL DEFAULT 0;
