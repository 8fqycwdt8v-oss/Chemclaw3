-- The reaction-label index: the derived, queryable view every faceted precedent question is asked
-- of (`chemclaw.science.labels`). NOT the record of truth — for an ELN reaction that is the
-- PR-gated note in git, for a patent corpus the source table. Both tables here are rebuildable
-- from those, and the only thing a rebuild costs is time.
--
-- Written in two phases, and that split is the design rather than an implementation detail. The
-- *record* phase is written by whoever ingested the reaction, from the canonical record in hand —
-- it has to be, because `OrdReaction.transformation_smiles()`, the string `reaction_fingerprints`
-- stores, deliberately drops solvent and catalyst (a solvent swap otherwise dominated DRFP
-- similarity: 0.82 for one coupling in THF vs 2-MeTHF, 1.00 once excluded). Right for a
-- fingerprint, fatal for an index whose whole job is to answer *which solvent, which ligand, which
-- base*. And `ElnAdapter` offers `fetch_new_entries(since)` and nothing that reads one entry back
-- by id, so there is no second chance to ask the source. The *derived* phase is filled later by
-- the background labeller, from this table's own `record_smiles`.
--
-- `labeller_version` is what separates them, and it is why "which entries are missing labels" is a
-- WHERE clause instead of a flag somebody has to remember to set: NULL means never derived, a
-- value below the current one means derived by a superseded labeller, and one indexed scan finds
-- both. Same idea as `note_index.fingerprint` (035) and `document_chunks.embedding_key` (038).
CREATE TABLE IF NOT EXISTS reaction_labels (
    -- `source` is part of the key and not decoration: two ELNs may legitimately use one entry id,
    -- which `reaction_fingerprints` — keyed on the bare id — cannot represent.
    source           TEXT NOT NULL,
    reaction_id      TEXT NOT NULL,

    -- record phase
    record_smiles    TEXT NOT NULL,   -- reactants>agents>products, agents KEPT
    citation         TEXT NOT NULL,   -- a note id for an ELN row, a patent number for a corpus row
    performed_on     DATE,
    temperature_c    DOUBLE PRECISION,
    time_h           DOUBLE PRECISION,
    yield_percent    DOUBLE PRECISION,
    workup_text      TEXT,            -- the one precedent question no structural index can answer

    -- derived phase
    mapped_smiles    TEXT,
    named_reaction   TEXT,
    reaction_class   TEXT,
    -- The vocabulary-independent key. NameRxn, Rxn-INSIGHT and RXNO are three different name
    -- strings for one transformation, so matching on the string answers from whichever fraction of
    -- the corpus happened to use that one — silently, and looking complete.
    rxno_id          TEXT,
    confidence       REAL,
    method           TEXT,            -- 'source' | 'smirks' | 'model'
    labeller_version TEXT,
    labelled_at      TIMESTAMPTZ,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, reaction_id)
);

-- The staleness scan is the drain's hot path — `WHERE labeller_version IS DISTINCT FROM $1 ORDER BY
-- source, reaction_id LIMIT n`, every batch, forever. Leading on the version makes it an index
-- range instead of a sequential scan over the whole corpus.
CREATE INDEX IF NOT EXISTS reaction_labels_staleness_idx
    ON reaction_labels (labeller_version, source, reaction_id);
-- Partial, because the facet queries only ever ask about rows that have a name, and on a
-- half-labelled corpus most rows do not.
CREATE INDEX IF NOT EXISTS reaction_labels_named_reaction_idx
    ON reaction_labels (named_reaction) WHERE named_reaction IS NOT NULL;
CREATE INDEX IF NOT EXISTS reaction_labels_rxno_idx
    ON reaction_labels (rxno_id) WHERE rxno_id IS NOT NULL;

-- One row per species per reaction: what it is, what the source called it, what a model concluded.
--
-- **No fingerprint bits here, deliberately.** A 13M-reaction corpus is ~65M of these rows over ~4M
-- distinct structures, so a per-row fingerprint is a 16x duplication and an HNSW index that will
-- not build; the bits live once per structure in `corpus_molecules` (051) and join by `smiles`,
-- which is already `standard_smiles` and therefore joins by value with no surrogate key. No
-- InChIKey, formula or molecular weight either — nothing asks, and this tree deletes dead columns.
--
-- `role` is the recorded `Role` verbatim; `derived_role` is the refined `SpeciesRole`, NULL until
-- a labeller has looked. NULL and 'unknown' are different answers: 'unknown' means a labeller
-- looked and could not decide, and conflating them would make an unlabelled corpus and an
-- unclassifiable one report identical coverage.
CREATE TABLE IF NOT EXISTS reaction_species (
    source            TEXT    NOT NULL,
    reaction_id       TEXT    NOT NULL,
    ordinal           INTEGER NOT NULL,
    smiles            TEXT    NOT NULL,
    role              TEXT    NOT NULL,
    derived_role      TEXT,
    -- A Bemis-Murcko scaffold buys an exact GROUP BY that similarity cannot, and the Ertl group
    -- names answer "a product containing this functional group" by array containment, with no
    -- SMARTS matching at query time.
    scaffold          TEXT,
    functional_groups TEXT[],
    PRIMARY KEY (source, reaction_id, ordinal)
);

-- "Has this substrate been used, and as what" — the leading column is the structure because that
-- is the half the question supplies.
CREATE INDEX IF NOT EXISTS reaction_species_structure_idx
    ON reaction_species (smiles, derived_role);
-- The mirror, for "which ligands were used at all": the role is supplied and the structures are
-- what is being counted.
CREATE INDEX IF NOT EXISTS reaction_species_role_idx
    ON reaction_species (derived_role, smiles);
-- The join back from a matched reaction to its whole recipe.
CREATE INDEX IF NOT EXISTS reaction_species_reaction_idx
    ON reaction_species (source, reaction_id);
CREATE INDEX IF NOT EXISTS reaction_species_groups_idx
    ON reaction_species USING gin (functional_groups);
CREATE INDEX IF NOT EXISTS reaction_species_scaffold_idx
    ON reaction_species (scaffold) WHERE scaffold IS NOT NULL;
