-- Indexes for browsing the calculation store (W2.2), not for addressing it.
--
-- 001 gave the table a primary key on the flat `key` and one index on
-- (calc_type, calc_version) — everything an exact cache lookup needs, and nothing a question
-- needs. "What have we already computed for this molecule" filters on `input_hash`, which had
-- no index at all, and "what has this calculator produced lately" orders by `created_at`, which
-- had none either. Both were sequential scans over the one table the system never evicts (D-011),
-- so they got slower for exactly as long as the deployment ran.
--
-- Applied by `make db-migrate` (idempotent). 001 is not edited; an applied migration never is.
CREATE INDEX IF NOT EXISTS calc_results_input_hash_idx
    ON calculation_results (input_hash);

-- DESC to match the query's own ORDER BY, so the newest-first page is a backwards-free read.
CREATE INDEX IF NOT EXISTS calc_results_created_at_idx
    ON calculation_results (created_at DESC);
