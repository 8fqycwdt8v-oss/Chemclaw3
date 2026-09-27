-- Which evidence tier a transcribed reaction is in
-- (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable).
--
-- `structured`: every species has a structure — fingerprinted, labelled and reachable by structure
-- and similarity search, as every row was before this column. `citation-only`: the source named at
-- least one species without giving its structure (the Perera flow-Suzuki screen's `2a, Boronic
-- Acid`), so the row is stored and citable for what it states and is excluded from every structure
-- and similarity search. The body says so in prose; this column is what a query, a count and the
-- structural readers' guard (`ReactionRecordStore.structurally_withheld`) read.
--
-- **`DEFAULT 'structured'` is a true statement about every existing row**, which is the question
-- a new column's default has to answer (`tasks/lessons.md` rule 89). Until this change the only
-- ingest path refused any reaction with a species it could not resolve to a structure
-- (`ord_adapter._smiles` raised, and every other adapter requires a SMILES per component), so no
-- stored row can hold a species without one. It is also true of what the *previous* image writes
-- during a rollout: that image still refuses such a reaction, so every row it upserts is
-- structured, and its `INSERT` naming no `tier` takes this default.
--
-- The upsert refreshes the column, so an amended entry moves between tiers with its body.
ALTER TABLE reaction_records
    ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'structured'
        CHECK (tier IN ('structured', 'citation-only'));

COMMENT ON COLUMN reaction_records.tier IS
    'structured: every species has a structure and the reaction is structure-searchable. '
    'citation-only: the source named a species without its structure; the row is citable for what '
    'it states and is withheld from every structure and similarity search.';
