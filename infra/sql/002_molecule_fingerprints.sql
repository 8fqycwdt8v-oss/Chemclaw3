-- Molecule fingerprint store (plan Phase 3, steps 3.2/3.3). One row per molecule:
-- its stable id, SMILES, and ECFP4 as a native bit string. Tanimoto similarity is
-- Jaccard on the bits, so an HNSW index with bit_jaccard_ops accelerates
-- nearest-neighbour search (pgvector >= 0.7). Applied by `make db-migrate`.
--
-- The bit width (2048) is coupled to `settings.ecfp_bits`: changing the configured
-- fingerprint width requires a matching schema change (a deliberate, rare event).
CREATE EXTENSION IF NOT EXISTS vector;

-- `label` is the human structure string (here a SMILES); the column is named neutrally
-- because the molecule and reaction fingerprint tables share one generic store.
CREATE TABLE IF NOT EXISTS molecule_fingerprints (
    id         TEXT        PRIMARY KEY,
    label      TEXT        NOT NULL,
    bits       bit(2048)   NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS molecule_fingerprints_jaccard_idx
    ON molecule_fingerprints USING hnsw (bits bit_jaccard_ops);
