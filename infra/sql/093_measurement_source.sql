-- A measurement is identified by the property, the molecule **and the source that reported it**
-- (D-2026-09-09-a-measurement-is-keyed-by-who-measured-it).
--
-- `030` keyed `measurements` on `(property, input_hash)` and argued the collapse in its own
-- comment: "A re-measurement replaces the row rather than accumulating, because two values for one
-- property of one molecule is a correction, not two facts." That is true of a lab correcting its
-- own typo. It is false of a replicate, a second solvent system, a repeat after a failed run, or a
-- second site — and `source` is the column that tells those apart. It was written and then used
-- only as the loser's epitaph, which is the same defect `051`, `056` and `063` found in three other
-- tables and fixed the same way.
--
-- Measured against a live database before the change, through the production `record_observation`
-- and `calibration_for`, on one prediction of -0.30 log S:
--
--     lab-basel    reports logS = -0.10   scored 1 prediction   n=1  bias = -0.200
--     lab-shanghai reports logS = -0.95   scored 1 prediction   n=1  bias = +0.650
--     measurements: [('solubility_logs', 'h-ethanol', -0.95, 'lab-shanghai')]
--
-- The bias a chemist reads for "how far off is this calculator" flipped sign on the arrival of a
-- second, equally valid number, at n=1 both times — so the count `Calibration`'s docstring says
-- exists to stop exactly this mistake ("a bias computed from three points is not a bias") reported
-- the honest 1 while concealing that a second observation had been taken and thrown away. Nothing
-- was logged and no counter moved.
--
-- The correction case `030` argued for is **kept, scoped to where it is true**: a second value from
-- the *same* source still replaces, because that is one reporter revising one number. What no
-- longer happens is one reporter silently deleting another's.
ALTER TABLE measurements DROP CONSTRAINT IF EXISTS measurements_pkey;
ALTER TABLE measurements ADD PRIMARY KEY (property, input_hash, source);

-- The consensus read (`science/calc/calibration.py::_CONSENSUS`) aggregates on the primary key's
-- leading columns, so it is served by the index the statement above builds — no second index here.

COMMENT ON COLUMN measurements.source IS
    'Who reported this value — a lab, an instrument, a run. Part of the row identity, because two '
    'sources measuring one property of one molecule are two facts and the ledger scores '
    'predictions against their consensus; a second value under the *same* source is a correction '
    'and replaces it. Rows written before migration 093 carry the empty string, which is one '
    'source like any other.';
