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
-- **A second table rather than more rows in `reaction_fingerprints`, and here the case is stronger
-- than it was for the molecules.** That table is keyed on the **bare** reaction id, which
-- `ingest_reaction`'s own docstring records as unable to tell two sources apart; a feed of millions
-- of rows would collide with this organisation's own ELN runs under shared entry ids *and* swamp
-- `similar_reactions` with hits whose `reaction-<id>` citation resolves to a different record. The
-- ELN table answers "have we run this?" and cites a transcription; this answers "is there
-- precedent?" and cites whatever the source gave.
--
-- **`id` is `<source>:<reaction_id>`**, so a hit joins to `reaction_labels (source, reaction_id)`
-- by construction — the same pair every other corpus table is keyed on, and the collision 051 and
-- 056 both exist to prevent.
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
    id         TEXT        PRIMARY KEY,   -- '<source>:<reaction_id>'
    label      TEXT        NOT NULL,      -- the transformation SMILES the bits were taken over
    bits       bit(2048)   NOT NULL,
    definition TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS corpus_reactions_jaccard_idx
    ON corpus_reactions USING hnsw (bits bit_jaccard_ops);
