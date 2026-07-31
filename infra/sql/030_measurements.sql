-- Measured values, kept whether or not anything predicted them yet (DARK-9).
--
-- `predictions` (migration 016) is prediction-first: `predicted_value` is `NOT NULL`, and
-- `record_observation` is a bare `UPDATE` against it. So a measurement for a molecule nothing had
-- predicted matched no row and was **discarded** — while `report_measurement` answered "Recorded
-- for <molecule>", which was simply untrue.
--
-- That is the common case, not an edge one. New chemistry is measured before it is predicted: a
-- chemist reports a solubility for a compound the system has never been asked about, and the one
-- fact worth keeping is thrown away. The ledger could then only ever learn from molecules the
-- agent happened to guess at first.
--
-- Keyed on `(property, input_hash)` — the same identity `predictions` uses, so a measurement and a
-- prediction of the same thing meet without a second naming scheme, and deliberately **not**
-- scoped by calculator version: a measurement is a fact about the molecule, not about whichever
-- calculator guessed at it. A re-measurement replaces the row rather than accumulating, because
-- two values for one property of one molecule is a correction, not two facts.
CREATE TABLE IF NOT EXISTS measurements (
    property     TEXT             NOT NULL,
    input_hash   TEXT             NOT NULL,
    -- The canonical SMILES, kept so a row is readable without joining back to a prediction that
    -- may not exist — which is the entire case this table was added for.
    subject      TEXT             NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    unit         TEXT             NOT NULL DEFAULT '',
    source       TEXT             NOT NULL DEFAULT '',
    observed_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),

    PRIMARY KEY (property, input_hash)
);
