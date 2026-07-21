-- Reaction fingerprint store (plan step 3.4, mcp-rxnfp). One row per reaction: its
-- stable id, the reaction SMILES (`label`), and its DRFP as a native bit string. The
-- schema mirrors molecule_fingerprints (002) so the generic fingerprint store serves both
-- tables. Tanimoto similarity is Jaccard on the bits, so an HNSW index with
-- bit_jaccard_ops accelerates search (pgvector >= 0.7). Applied by `make db-migrate`.
--
-- The bit width (2048) is coupled to `settings.drfp_bits`: changing the configured DRFP
-- width requires a matching schema change (a deliberate, rare event).
CREATE EXTENSION IF NOT EXISTS vector;

-- `definition` records the DRFP parameters that produced `bits` (e.g. `drfp:b2048`); similarity
-- search filters to one definition so a width change + re-index can't rank incomparable rows (004).
CREATE TABLE IF NOT EXISTS reaction_fingerprints (
    id         TEXT        PRIMARY KEY,
    label      TEXT        NOT NULL,
    bits       bit(2048)   NOT NULL,
    definition TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reaction_fingerprints_jaccard_idx
    ON reaction_fingerprints USING hnsw (bits bit_jaccard_ops);
