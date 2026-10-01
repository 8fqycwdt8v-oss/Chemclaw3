-- Each compared role's canonical structures ride on the reaction row, so the turn-time comparison
-- can diff what the ELN gives structured (D-2026-10-01-the-turn-time-comparison-reads-the-species-the-row-keeps).
--
-- `agent.condense._changes` could diff temperature, time and a solvent read out of prose, and
-- nothing else: the component list survived only as prose inside `body`. The mined campaign note
-- diffs every role's species set off `OrdReaction` (`memory.progression.changes_between`), so on
-- the one schema where the components are the most reliable thing a source provides, the table a
-- chemist is answered with in the turn was the one that could not use them. Measured on the
-- seeded corpus: 4,150 of 4,175 adjacent campaign pairs moved a species and no setpoint.
--
-- **A projection, not the charge list** (`ingest.eln.ord.RoleSpecies`): structures per role, no
-- amounts and no order, so the row stays a serving copy of what the source said rather than a
-- second transcription of it. Measured at a mean of 217 bytes a row against 1,235 for the row
-- it rides on.
ALTER TABLE reaction_records ADD COLUMN IF NOT EXISTS species JSONB;

-- No index, for `053`'s reason: read only as part of a row already located by its key.
COMMENT ON COLUMN reaction_records.species IS
    'The canonical SMILES playing each compared role (ingest.eln.ord.RoleSpecies), as JSONB. NULL '
    'means no projection was stored (a row ingested before this column, or a source that gave no '
    'components) and the comparison skips it; an empty list is the record saying the run used '
    'nothing in that role, which is a real change and is diffed.';
