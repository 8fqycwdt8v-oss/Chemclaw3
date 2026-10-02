-- The citation-only records, indexed by themselves, so every structural tool can say how many sit
-- outside its search (`ReactionRecordStore.citation_only`).
--
-- A citation-only record contributes no fingerprint, molecule or label row
-- (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable), so a structure search
-- that reports "complete" over its own index reads as a search of the ELN. On the 2026-10-02 lane
-- the indexes were complete, `substrate_precedent` said "COMPLETE: all 4282", and the model told a
-- chemist there was no in-house data on 6-iodoquinoline while the flow-Suzuki runs sat outside
-- the count — among them `reaction-suzuki-flow-hte-01243`, 6-iodoquinoline drawn and only its
-- coupling partner named. Each structural verdict now counts the tier and looks for the queried
-- structure among its drawn species.
--
-- **Partial, on exactly the predicate the read uses** (`tier = 'citation-only' AND retracted_at IS
-- NULL`). The read runs on every structural tool call, and without this it is a sequential scan of
-- `reaction_records`, which the inventory sizes at a few GB for a 3M-entry ELN. With it the read
-- touches only the tier's own rows.
-- Keyed on the table's primary key so it carries nothing the row does not already. Measured on a
-- synthetic 500,000-row table with 5% in the tier and ~1.2 kB bodies, five runs each: a parallel
-- sequential scan at 83,410 buffers and 255-427 ms without it, an index scan at 25,097 buffers and
-- 117-206 ms with it. What remains is reading the tier's own rows.
--
-- `IF NOT EXISTS` so the file replays like every other in this directory.
CREATE INDEX IF NOT EXISTS reaction_records_citation_only_idx
    ON reaction_records (ingest_source, reaction_id)
    WHERE tier = 'citation-only' AND retracted_at IS NULL;
