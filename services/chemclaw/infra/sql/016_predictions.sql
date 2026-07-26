-- Predicted-vs-actual ledger (gap IDEA-2).
--
-- The stack predicts (xTB, pKa, solubility, BO surrogates) and, separately, ingests what actually
-- happened (the ELN). Nothing closed the loop. `evals/metrics.py::prediction_error` exists but
-- scores against a *held-out reference* in a committed case file — not against reality as it
-- arrives — so "how far should I trust this calculator?" (the whole job of the
-- calculation-selection skill) was answerable only in prose.
--
-- One row per prediction, with the observation filled in later if and when one arrives. The
-- (calc_type, input_hash) pair is the same identity the calculation cache keys on, so a prediction
-- and a measurement of the same thing meet without a second naming scheme.
--
-- `predicted_uncertainty` is stored because calibration is not only about bias: a calculator whose
-- errors are small but whose stated uncertainty never covers them is miscalibrated in a way a mean
-- error cannot show.
CREATE TABLE IF NOT EXISTS predictions (
    id                    BIGSERIAL   PRIMARY KEY,
    calc_type             TEXT        NOT NULL,
    calc_version          TEXT        NOT NULL,
    input_hash            TEXT        NOT NULL,
    subject               TEXT        NOT NULL,
    predicted_value       DOUBLE PRECISION NOT NULL,
    predicted_uncertainty DOUBLE PRECISION,
    unit                  TEXT        NOT NULL DEFAULT '',
    predicted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    observed_value        DOUBLE PRECISION,
    observed_at           TIMESTAMPTZ,
    observed_source       TEXT
);

-- One prediction per (calc_type, version, input): re-predicting the same thing must update the row
-- rather than accumulate duplicates that would then be double-counted in the calibration.
CREATE UNIQUE INDEX IF NOT EXISTS predictions_identity
    ON predictions (calc_type, calc_version, input_hash);

-- The calibration query reads only reconciled rows per calculator.
CREATE INDEX IF NOT EXISTS predictions_reconciled
    ON predictions (calc_type) WHERE observed_value IS NOT NULL;
