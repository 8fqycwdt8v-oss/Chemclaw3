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
-- Additive and **nullable** throughout, per `infra/sql/README.md`: every existing row keeps its
-- meaning and the previous image can still write, because nothing here is NOT NULL. Nullable
-- rather than defaulted is a second decision on top of that one, argued at each column below —
-- a default would give a row written before the measurement existed a value that reads as a
-- measurement.

-- Tool calls this turn made against the knowledge record (`authz.knowledge_read_tools()`) and
-- against the knowledge write surface (`authz.KNOWLEDGE_WRITE_TOOLS`). Zero is a real, common and
-- interesting value on both: a turn that answered without looking, and a turn that learned
-- something and kept it to itself.
--
-- **Which is exactly why they are nullable and undefaulted**, and the first draft of this file got
-- it wrong in the two directions its own next paragraph argues against. `NOT NULL DEFAULT 0`
-- backfills every row `turn_costs` has ever held with the most interesting value these columns can
-- take: a query for "turns that answered without consulting the record" would return the entire
-- history of this table, none of which was measured. A turn written by the image that added these
-- columns always supplies a real number (`TurnCost` defaults them to 0 in Python, and
-- `turn_cost_store._COLUMNS` writes all five), so NULL means precisely one thing — the row predates
-- the measurement — and it is the reading `D-2026-08-03-a-metric-must-declare-what-it-can-see`
-- requires: a column that cannot see something must say so rather than report a zero.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS retrieval_calls INTEGER;
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS capture_calls   INTEGER;

-- The verifier's aggregate citation-faithfulness score in [0, 1], or NULL when it did not run.
--
-- **Nullable on purpose, and it must not be defaulted to 0.** `verifier_enabled` can be off, and a
-- turn routed to review by the deterministic answer-shape gate carries `review_required = true`
-- with no score at all. Storing a 0 there would read as "graded, and graded terrible" for a turn
-- that was never graded — the ambiguous zero `D-2026-08-03-a-metric-must-declare-what-it-can-see`
-- is about, in a column.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS answer_confidence DOUBLE PRECISION;

-- Nullable for the same reason, one type over: `FALSE` is "this answer needed no review", which is
-- a finding, and defaulting it would assert that finding about every turn taken before anything
-- recorded it.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS review_required BOOLEAN;

-- How many `[[note ids]]` the answer cited. The join from "we retrieved" to "we used it": a turn
-- with `retrieval_calls > 0` and `notes_cited = 0` searched the record and then answered from
-- somewhere else, which is a different failure from never having looked.
ALTER TABLE turn_costs ADD COLUMN IF NOT EXISTS notes_cited INTEGER;
