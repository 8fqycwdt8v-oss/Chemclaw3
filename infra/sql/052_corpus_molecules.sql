-- The external corpus's molecules, deduplicated by standardized structure.
--
-- Deliberately the same five columns `molecule_fingerprints` (002/004) carries, so
-- `PostgresFingerprintStore("corpus_molecules", settings.ecfp_bits, molecule_definition())` serves
-- similarity search over it with **no new code at all** — that class is already
-- table-parameterised, which is what the D-011-era extraction bought.
--
-- Deliberately a second *table* rather than more rows in the first, because the two answer
-- different questions and cite different things: `molecule_fingerprints` is "have we made this?"
-- and its hits cite a compound note, this is "is there literature precedent?" and its hits cite a
-- patent. Merging them would swamp the ELN corpus by four orders of magnitude and hand
-- `similar_molecules` millions of hits whose `compound_note_id` resolves to nothing.
--
-- Joined to `reaction_species` by `smiles`, which is `core.chem.standard_smiles` on both sides —
-- so the join is by value and there is no surrogate key to keep in step.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS corpus_molecules (
    id           TEXT      PRIMARY KEY,   -- the standardized SMILES
    label        TEXT      NOT NULL,
    bits         bit(2048) NOT NULL,
    definition   TEXT      NOT NULL,
    -- The set-bit indices of an RDKit PatternFingerprint. An INTEGER[] and not a bit string,
    -- because substructure screening is bitwise *containment* and GIN's `@>` is the only index in
    -- stock Postgres that answers it — pgvector's HNSW ranks by distance and cannot. Sound in one
    -- direction only, which is exactly what a screen needs: a molecule missing any of the query
    -- pattern's bits cannot contain the pattern, so skipping it is safe, and the survivors are
    -- verified exactly with RDKit afterwards.
    --
    -- ECFP bits cannot do this. `docs/planning/DEFERRED.md` says so in as many words, and the
    -- reason is that a Morgan fingerprint hashes whole circular environments: a substructure's
    -- environment is not a subset of the environments of a molecule that contains it, so a
    -- containment test over ECFP bits drops true hits.
    pattern_bits INTEGER[],
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corpus_molecules_jaccard_idx
    ON corpus_molecules USING hnsw (bits bit_jaccard_ops);
CREATE INDEX IF NOT EXISTS corpus_molecules_pattern_idx
    ON corpus_molecules USING gin (pattern_bits);
