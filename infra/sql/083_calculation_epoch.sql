-- The `CALCULATION_EPOCH` a stored calculation was written under
-- (D-2026-09-05-an-epoch-inside-a-hash-protects-the-cache-and-not-the-record).
--
-- **The cache was already right; the record and the browse surface were not.** The epoch is folded
-- into `params_hash` (`CalculationKey.build`, and `connectors/calc/remote.py::remote_key` for every
-- `calc` row), so an exact-key `get` cannot serve an epoch-1 payload to an epoch-2 caller — a 3x3
-- client/server epoch grid produces nine distinct keys. But `find_calculations` browses instead of
-- addressing, and it served epoch-1 rows beside epoch-2 rows for the same subject, distinguishable
-- only by `created_at`. Per the epoch log those epoch-1 rows carry a wrong linear-rotor entropy and
-- free energy and an incomplete per-atom reactivity panel. A reader cannot act on a difference that
-- is not on the row.
--
-- **Empty means "not recorded", which is a third state and not a synonym for "old".** Nothing can
-- recover the epoch of a row written before this column existed: it is inside an opaque digest.
-- Backfilling the current value would assert of every historical row exactly the thing the column
-- exists to stop being assumed, and backfilling '1' would assert it of rows that may well be
-- current. So the default is the empty string and the reader reports three states rather than two.
--
-- Additive and defaulted, so the previous image keeps writing this table unchanged
-- (`tests/test_migrations_are_additive.py`). Applied by `make db-migrate` (idempotent).
ALTER TABLE calculation_results
    ADD COLUMN IF NOT EXISTS epoch TEXT NOT NULL DEFAULT '';

-- No index. Every question this column answers is asked *about rows already selected* by `find`'s
-- existing predicates — "is the row I am looking at current?" — and the one query that could scan
-- on it (an operator counting stale rows) is a maintenance question, not a hot path. An index that
-- serves no query is a write cost on the one table D-011 never prunes.
