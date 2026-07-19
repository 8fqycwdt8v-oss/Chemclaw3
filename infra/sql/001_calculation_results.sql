-- Calculation result store (plan Phase 1b, D-011). One row per unique
-- (calculator version + input + params); the flat `key` is the primary key so an
-- upsert is a single statement and lookups are O(1). Applied by `make db-migrate`
-- (idempotent). Keeping the component columns alongside the key makes results
-- queryable by calculator/version for later analysis without parsing the key.
CREATE TABLE IF NOT EXISTS calculation_results (
    key          TEXT PRIMARY KEY,
    calc_type    TEXT        NOT NULL,
    calc_version TEXT        NOT NULL,
    input_hash   TEXT        NOT NULL,
    params_hash  TEXT        NOT NULL,
    result       JSONB       NOT NULL,
    provenance   TEXT        NOT NULL DEFAULT 'computed',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS calc_results_type_version_idx
    ON calculation_results (calc_type, calc_version);
