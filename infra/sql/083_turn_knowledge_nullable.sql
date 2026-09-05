-- The knowledge columns 082 added, made nullable: a default is an assertion about every row that
-- already exists.
--
-- 082 declared `retrieval_calls`, `capture_calls`, `notes_cited` and `review_required` as
-- `NOT NULL DEFAULT`. Zero is the most interesting value those columns can take — a turn that
-- answered without consulting the record, a turn that learned something and kept it to itself — so
-- the default backfills exactly that finding onto every row `turn_costs` has ever held, and a query
-- for "turns that answered blind" returns the table's whole history, none of which was measured.
--
-- 082's own next paragraph argued this, about `answer_confidence`, and then did the opposite one
-- column over: "a 0 there would read as 'graded, and graded terrible' for a turn that was never
-- graded — the ambiguous zero `D-2026-08-03-a-metric-must-declare-what-it-can-see` is about, in a
-- column." Four columns, one argument, applied to one of them.
--
-- **A separate file rather than an edit to 082, and the rule is worth stating.** `core/migrate.py`
-- keys on a checksum of a migration's statements and refuses to run when one changes, so editing a
-- file that has been applied breaks `make db-migrate` on every database holding it — including the
-- dev database this branch's own author had already migrated. `tests/test_migrations_are_additive.py`
-- asks git the same question and is what caught it here. That a migration is unmerged is not the
-- test's question and must not be: "who has applied it" is unknowable from inside the repository,
-- which is why the rule is mechanical.
--
-- A turn written by an image that has these columns always supplies a value (`TurnCost` defaults
-- them in Python, `turn_cost_store._COLUMNS` writes all five), so after this NULL means precisely
-- one thing: the row predates the measurement.

ALTER TABLE turn_costs ALTER COLUMN retrieval_calls DROP NOT NULL;
ALTER TABLE turn_costs ALTER COLUMN retrieval_calls DROP DEFAULT;
ALTER TABLE turn_costs ALTER COLUMN capture_calls   DROP NOT NULL;
ALTER TABLE turn_costs ALTER COLUMN capture_calls   DROP DEFAULT;
ALTER TABLE turn_costs ALTER COLUMN notes_cited     DROP NOT NULL;
ALTER TABLE turn_costs ALTER COLUMN notes_cited     DROP DEFAULT;
ALTER TABLE turn_costs ALTER COLUMN review_required DROP NOT NULL;
ALTER TABLE turn_costs ALTER COLUMN review_required DROP DEFAULT;

-- The rows 082's default already wrote a measurement onto. `ADD COLUMN ... NOT NULL DEFAULT 0`
-- backfills, so a deployment with a year of `turn_costs` behind it gets the fabricated zero on
-- every historical row — dropping the default afterwards does not take it back off them. That is
-- the case this statement exists for, and the two migrations ship in one pull request, so a
-- deployment applies them in one pass with no turn between.
--
-- **The predicate cannot be exact, and the residual case is stated rather than denied.** A row this
-- migration nulls is indistinguishable from a genuine turn that searched nothing, wrote nothing,
-- cited nothing and was never graded: both are four zeros and a NULL confidence. Such a row can
-- only exist on a database that applied 082, ran turns, and then applied 083 — which is a
-- developer's own database on this branch, never a deployment, because 082 has never been on
-- `main` without 083 beside it. Losing a handful of rows there is the smaller error than leaving
-- every production turn ever recorded reading as "answered without consulting the record", which is
-- the reading this whole migration exists to prevent.
UPDATE turn_costs
   SET retrieval_calls = NULL,
       capture_calls   = NULL,
       notes_cited     = NULL,
       review_required = NULL
 WHERE retrieval_calls = 0
   AND capture_calls   = 0
   AND notes_cited     = 0
   AND review_required = FALSE
   AND answer_confidence IS NULL;
