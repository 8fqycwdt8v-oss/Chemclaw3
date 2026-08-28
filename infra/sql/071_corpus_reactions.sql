-- The corpus's reactions, as DRFP bits: the half `054_corpus_molecules.sql` did for structures
-- (D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop).
--
-- `drain_corpus` has always written the species into `reaction_species` and their structures into
-- `corpus_molecules`, so a bulk reaction source arrived searchable by *molecule* similarity and not
-- by *reaction* similarity. `record_for_reaction` — the DRFP write — had exactly one caller in the
-- tree, on the ELN path. This is the missing table.
--
-- Deliberately the same five columns `reaction_fingerprints` (003/004) carries, so
-- `PostgresFingerprintStore("corpus_reactions", settings.drfp_bits, reaction_definition())` serves
-- similarity over it with **no new search code at all** — the property `corpus_molecules` was built
-- for, applied to the other half.
--
-- **A second table rather than more rows in `reaction_fingerprints`, and the case is narrower than
-- it first looks.** One half of it is gone: `063_reaction_fingerprint_source.sql` gave that table a
-- `source` column and an `(source, id)` primary key
-- (`D-2026-08-27-a-fingerprint-is-keyed-by-its-source`), so it can now tell two sources apart and
-- "a feed would collide with this organisation's ELN runs" is no longer a reason for anything.
--
-- What survives is the argument `054_corpus_molecules.sql` rests on, unchanged by that fix: the two
-- tables answer different questions and cite different things. `reaction_fingerprints` is "have we
-- run this?" and its hits resolve to a `reaction-<id>` transcription; this is "is there literature
-- precedent?" and its hits cite whatever the source gave. Merging them would swamp
-- `similar_reactions` by four orders of magnitude with hits whose note id resolves to nothing —
-- which is exactly why the molecule halves are two tables and not one.
--
-- **Keyed `(source, id)` like `reaction_fingerprints` now is**, with `id` the source's own reaction
-- id rather than a `<source>:<id>` string this table would have to compose and every reader
-- decompose. A first draft did compose one, before `063` existed; `source` as a column is the
-- house pattern and it joins to `reaction_labels (source, reaction_id)` without string surgery.
--
-- **`label` is the transformation form, `reactants>>products`.** The agent slot is dropped before
-- the bits are taken, because `DrfpEncoder` folds agents onto the reactants — so keeping them lets
-- a solvent swap dominate similarity, measured at 0.82 for one coupling in THF vs 2-MeTHF and 1.00
-- once excluded (`ingest/eln/ord.py`). Nothing is lost: the agents are rows in `reaction_species`,
-- which is the index built to answer *which solvent, which ligand, which base*.
--
-- The bit width (2048) is coupled to `settings.drfp_bits`, exactly as 003 is.
--
-- Applied by `make db-migrate` (idempotent).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS corpus_reactions (
    -- The registry source name (`CHEMCLAW_DATA_SOURCES`) — the same string `reaction_labels.source`
    -- and `reaction_fingerprints.source` carry, so every index an ingest writes agrees on what a
    -- source is. Not defaulted: unlike `063`, this table is new, so there is no unsourced
    -- population to backfill and no reason to permit one.
    source     TEXT        NOT NULL,
    id         TEXT        NOT NULL,      -- the source's own reaction id
    label      TEXT        NOT NULL,      -- the transformation SMILES the bits were taken over
    bits       bit(2048)   NOT NULL,
    definition TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (source, id)
);

CREATE INDEX IF NOT EXISTS corpus_reactions_jaccard_idx
    ON corpus_reactions USING hnsw (bits bit_jaccard_ops);
