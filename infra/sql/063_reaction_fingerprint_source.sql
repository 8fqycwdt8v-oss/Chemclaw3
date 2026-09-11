-- A reaction fingerprint is identified by its ingest source *and* the entry id
-- (D-2026-08-27-a-fingerprint-is-keyed-by-its-source).
--
-- `003` keyed `reaction_fingerprints` on the bare `id`, which is the ELN's own entry id. Two ELNs
-- may legitimately use one entry id — `051` put `(source, reaction_id)` in `reaction_labels`'
-- primary key for exactly that reason, and `056` did the same for `reaction_records` — and this
-- table was the one index left that could not tell the two sites apart. `ingest_reaction`'s
-- docstring said so in the tree's own words: "this function writes four indexes and only three of
-- them can tell the two sites apart."
--
-- Measured against a live database before the change, ingesting `EXP-1001` from `eln-a` (an
-- esterification) and then from `eln-b` (a bromination):
--
--     reaction_fingerprints rows: 1     -- ('EXP-1001', 'c1ccccc1.BrBr>>Brc1ccccc1')
--     reaction_records rows:      2     -- [('eln-a','EXP-1001'), ('eln-b','EXP-1001')]
--     search for site A's own reaction: []
--       verdict: "No indexed reaction matched this query. The reaction fingerprint index holds
--                 records and was searched, so this is a genuine negative result."
--
-- Site A's transcription survived (`056`); its *structure* did not. The one tool whose whole job is
-- "have we seen this before" answered "a genuine negative result" about a reaction it had been
-- handed seconds earlier — the exact failure `FingerprintSearch.verdict` exists to prevent, arriving
-- through the key instead of through an empty index.
--
-- **`source` is the registry source name** (the token in `CHEMCLAW_DATA_SOURCES`), the same string
-- `reaction_labels.source` and `reaction_records.ingest_source` carry, so the four indexes agree on
-- what a source is. Named `source` rather than `ingest_source` because this table has no other
-- claimant on the word — `reaction_records` needed the longer name only because its `source` column
-- already held a rendered provenance string.
ALTER TABLE reaction_fingerprints ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT '';

-- **The rows already stored are backfilled exactly, not guessed.** `056` had to default its column
-- to `''` because nothing in a `reaction_records` row said which source wrote it. Here something
-- does, in the same database: `ingest_reaction` writes the label index's record phase and the
-- fingerprint row from one call, so every fingerprint row ingested since `051` has a
-- `reaction_labels` row carrying its true source. Claiming an id under exactly one source is the
-- condition — an id two sources claim is the collision itself, and one surviving fingerprint row
-- could belong to either, so it is left alone rather than assigned to the alphabetically-first.
--
-- What stays `''` afterwards is therefore bounded and named: rows ingested before `051` existed,
-- and rows whose id two sources already claim.
--
-- This comment used to continue: "Both are superseded on the next ingest —
-- `PostgresFingerprintStore.add` deletes the unsourced twin when it writes a sourced row, so no
-- entry is ever searchable twice under one label." That was never true and is not true now:
-- `store.py` contains no `DELETE` at all, and the runtime role holds none on either fingerprint
-- table, which `store.py`'s own constructor explains at length. An unsourced twin is *shelved*
-- beside its sourced row rather than removed, and `094` made that the key's shape for every
-- generation. So an entry can be searchable twice under one label, and what stops the two being
-- read as independent is the definition scoping in the reader, not a delete here.
--
-- The statements below are unchanged, and a merged migration's statements are what is immutable:
-- the runner hashes `_statements`, comments stripped, precisely so a wrong comment can be
-- corrected without refusing on every database that already applied the file.
--
-- This index is derived and rebuildable; unlike a transcription there is nothing here a re-sync
-- cannot regenerate.
UPDATE reaction_fingerprints f
SET source = claimant.source
FROM (
    SELECT reaction_id, min(source) AS source
    FROM reaction_labels
    GROUP BY reaction_id
    HAVING count(*) = 1
) AS claimant
WHERE f.source = '' AND f.id = claimant.reaction_id;

-- `ADD PRIMARY KEY` builds a unique index under an ACCESS EXCLUSIVE lock, as `041` and `056` did.
-- It also ends the "deploy the previous image" rollback: that image's `ON CONFLICT (id)` no longer
-- matches a constraint and every fingerprint write fails to plan. The ADR states what an operator
-- does instead, and `tests/test_migrations_are_additive.py` holds the exemption to the statements.
ALTER TABLE reaction_fingerprints DROP CONSTRAINT IF EXISTS reaction_fingerprints_pkey;
ALTER TABLE reaction_fingerprints ADD PRIMARY KEY (source, id);

COMMENT ON COLUMN reaction_fingerprints.source IS
    'The registry source name that ingested this reaction — half of the row identity, because two '
    'ELNs may use one entry id and the site that synced last must not make the other site''s '
    'chemistry unfindable. Empty on rows written before migration 063 that no single-claimant '
    'label row could resolve; a sourced write supersedes its unsourced twin.';
