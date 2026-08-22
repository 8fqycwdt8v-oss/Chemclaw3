-- Which 3-D geometry a stored calculation ran on
-- (D-2026-08-21-a-geometry-is-an-address-not-a-payload).
--
-- **The question this makes answerable.** `find_calculations` refuses a molecule filter on the
-- structure-keyed families, and rightly: `input_hash` is a digest over a *geometry*, and a molecule
-- does not determine one. But nothing replaced it, so "have we already relaxed this conformer?" —
-- the lookup D-011's whole cache exists to serve, and the one a chemist makes before committing
-- hours of compute — had no query at all. The only remaining filter was `calc_type` alone, which
-- returns the most recent rows across every molecule in the deployment.
--
-- **The server's own answer, not a derived one.** `calculation_key` reports `structure_id` for the
-- calculations that run on a geometry and the client was dropping it. Deriving one here instead
-- would be the mistake `connectors/calc/remote.py` records for `calc_version`: well-formed, and
-- matching nothing.
--
-- **The geometry it ran *on*, never the one it produced.** That is the server's own meaning and it
-- is the useful one: a chemist holding conformer #3's address asks what has already been computed
-- at it — the relaxation started from it, its properties, its Hessian — and a column naming
-- outputs could answer none of that.
--
-- Empty for a molecule-keyed calculator (pKa, solubility, descriptors) and for every row written
-- before this migration, which is why the filter is an equality on a non-empty value rather than a
-- three-way — an absent value means "not recorded", and answering a geometry question with rows
-- that never claimed one would be worse than answering with none.
--
-- Additive and nullable-by-default, so the previous image keeps writing this table unchanged
-- (`tests/test_migrations_are_additive.py`). Applied by `make db-migrate` (idempotent).
ALTER TABLE calculation_results
    ADD COLUMN IF NOT EXISTS structure_id TEXT NOT NULL DEFAULT '';

-- Partial, because the column is empty on every molecule-keyed row and on every row that predates
-- it: indexing those would be indexing one value. Ordered by `created_at` inside the geometry, so
-- the newest-first scan `find` performs is served by the index rather than by a sort.
CREATE INDEX IF NOT EXISTS calculation_results_structure_idx
    ON calculation_results (structure_id, created_at DESC)
    WHERE structure_id <> '';
