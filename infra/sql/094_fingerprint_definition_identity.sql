-- A fingerprint row is identified by its key *and* the definition that produced its bits
-- (D-2026-09-09-a-definition-change-shelves-a-row-it-does-not-delete).
--
-- `004` added the `definition` column and stated the safety property it buys: "a mismatched
-- backfill only makes stale rows fall out of similarity search (safe), never returns a wrong
-- score". `science/fingerprints/store.py` repeated it — "the stale rows simply fall out of search
-- until they are re-indexed". Both sentences describe rows that still exist. They did not: the
-- primary key was `id` alone (`(source, id)` since `063`), `definition` was an ordinary column,
-- and the upsert's `DO UPDATE SET … definition = EXCLUDED.definition` overwrote it. Measured
-- against a live database, two stores over one table:
--
--     after writer A (ecfp:r2:b2048):  rows=1   A.count=1   B.count=0
--     after writer B (ecfp:r3:b2048):  rows=1   A.count=0   B.count=1
--     table now: [('CCO', 'ethanol@B', 'ecfp:r3:b2048')]
--     A superseded_count: 1   B superseded_count: 0
--
-- Inside one deployment mid-rebuild that is invisible, because the re-index is walking those rows
-- anyway. It stops being invisible the moment two writers hold different definitions at once — a
-- rolling upgrade that changes `ecfp_radius`, two pods on different images, a second site: each
-- write evicts the other's row, each side's `superseded_count` reports the *other's* population as
-- "stale, re-index me", and neither index ever converges.
--
-- With the definition in the key the two generations coexist and neither can overwrite the other,
-- while a re-write under one definition still updates in place. This is the shape `041` gave
-- `document_chunks` with `chunking_key` and `039` gave `note_index` with its embedding key: a
-- derived row is identified by what derived it.
--
-- **Three tables, and that is every table `PostgresFingerprintStore` writes.** `corpus_molecules`
-- is deliberately absent: it is written by `science/labels/molecules.py::CorpusMolecules`, whose
-- own `INSERT … ON CONFLICT (id)` would fail to plan against a widened key, so it keeps the
-- single-generation behaviour until that statement moves with it.
--
-- **What it costs is a row that nothing in this system can remove.**
-- `infra/sql/grants/app_privileges.sql` grants the runtime role INSERT and UPDATE on these tables
-- and withholds DELETE — which is what makes `durable/retention.py`'s refusal to prune enforced
-- rather than intended. So a completed rebuild now *leaves the superseded generation on the shelf*:
-- the table holds both, `superseded_count()` keeps reporting the old one, and every search says
-- PARTIAL until an operator disposes of it under the owning principal, beside `make db-migrate`:
--
--     DELETE FROM molecule_fingerprints WHERE definition <> '<the current definition>';
--
-- That is the same standing as `chemclaw.cli.rekey_campaigns` — an operator's one-off run under the
-- principal that owns the schema, rather than a verb granted to a chat turn for the life of the
-- deployment. It is stated here because it is a new step in the re-index procedure, not a detail.
--
-- The second half of that bill is disk and it is the one an operator will feel first: a shelved
-- generation is a second full copy of the index (a `bit(2048)` row plus its label), so a bump on a
-- Pistachio-scale corpus holds two of them until the disposal runs. On the `approximate` search arm
-- it is also a permanent halving of the over-fetch, because the inner `ORDER BY … LIMIT` cannot see
-- the definition and every shelved row takes a candidate slot — `store.py` says so beside that
-- statement. The shipped arm is `exact`, whose `WHERE definition` is inside the scan.
--
-- **`ADD PRIMARY KEY` builds a unique index under an ACCESS EXCLUSIVE lock** (the migrator's 5 s
-- `lock_timeout` bounds waiting for the lock, not the build), as `041`, `056` and `063` did. It
-- also ends the "deploy the previous image" rollback on all three tables: that image's
-- `ON CONFLICT (id)` / `ON CONFLICT (source, id)` no longer matches a constraint and every
-- fingerprint write fails to plan. The ADR states what an operator does instead, and
-- `tests/test_migrations_are_additive.py` holds the exemption to the statements.
ALTER TABLE molecule_fingerprints DROP CONSTRAINT IF EXISTS molecule_fingerprints_pkey;
ALTER TABLE molecule_fingerprints ADD PRIMARY KEY (id, definition);

ALTER TABLE reaction_fingerprints DROP CONSTRAINT IF EXISTS reaction_fingerprints_pkey;
ALTER TABLE reaction_fingerprints ADD PRIMARY KEY (source, id, definition);

ALTER TABLE corpus_reactions DROP CONSTRAINT IF EXISTS corpus_reactions_pkey;
ALTER TABLE corpus_reactions ADD PRIMARY KEY (source, id, definition);

COMMENT ON COLUMN molecule_fingerprints.definition IS
    'The fingerprint parameters that produced `bits` (e.g. `ecfp:r2:b2048`) — half of the row '
    'identity, so a definition change shelves the generation it supersedes instead of deleting '
    'it. The runtime role holds no DELETE here: disposing of a shelved generation after a '
    'completed rebuild is an operator statement run under the owning principal.';
