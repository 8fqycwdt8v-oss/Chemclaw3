-- The knowledge graph's notes as rows (D-2026-10-14-the-notes-live-in-postgres-and-git-leaves-the-
-- knowledge-path). A note keeps its format: a frontmatter header and a Markdown body. The header
-- is JSONB here instead of YAML in a file; the body is text.
--
-- This migration is the *expand* step. Nothing reads or writes these tables until the Postgres
-- store ships (`kg/store.py`, setting `kg_store`); notes stay in git until then, and a release can
-- be rolled back by leaving the setting alone. Migrating the corpus in (`cli/kg_import`) and
-- making `note_index` follow the foreign key below are the next two steps, each its own release.
-- Nothing is dropped, in this file or later: the git-era columns of `note_index` are left unread.
--
-- No `GRANT` yet, deliberately: `grants/app_privileges.sql` is checked against the SQL `src/`
-- executes (`tests/test_database_privileges.py`), so the write verbs are granted in the change
-- that first writes. They will be INSERT on `kg_note_revisions` only (append-only by grant, as
-- `audit_events` is), INSERT and UPDATE on `kg_notes`, and INSERT and DELETE on `kg_edges`, whose
-- rows are re-derived whenever a note is revised.
--
-- **Every table is keyed by `tenant_id` first.** Tenancy is `tenant_id` plus row-level security
-- (D-2026-10-07-the-architecture-programme), and a primary key or foreign key on `id` alone cannot
-- be widened later without dropping it, which a forward-only schema refuses. Deployments are one
-- tenant today, so the column defaults to 'default' and no code writes it; the policies come with
-- the tenancy wave.

-- One row per note id: its current revision. `id` and `type` become file names when the corpus is
-- exported (`cli/kg_export`), so they follow `kg.note.require_note_slug` exactly: a plain slug,
-- no `..`, no trailing `.` and no `.lock` suffix (the last two were git ref rules and stay, so an
-- exported corpus is one git can hold).
--
-- `frontmatter` holds every field of `kg.note.Note` that is not a column (`compound_smiles`,
-- `tags`, `actor`, `source`, `confidence`, `conditions`, `calc_refs`, `artifact_refs`,
-- `relations`), in the shape `Note.model_dump(mode="json", exclude_none=True)` gives. A field the
-- schema grows lands there without a migration. `created_by`, the validity window and the type are
-- columns because they are filtered on and constrained.
--
-- `content_sha` is `sha256:<hex>` of the canonical render: `render_note` with this process's
-- credentials redacted, which is the text `kg.record` writes to a file (`kg/rows.py`). It is NOT
-- the fingerprint `note_index` holds today, which is the digest of a file's bytes and so also
-- depends on how a person laid the file out: the two agree for a note the writer produced and
-- differ for a hand-formatted one. A database store's `fingerprints()` answers with this column,
-- so the index is consistent within the store, and the flip re-embeds each note once.
--
-- `validity` is the window as a range, inclusive at both ends and open where a bound is absent,
-- which is `TemporalWindow.is_current` exactly: `validity @> as_of` replaces its two comparisons.
-- Building the range is also what refuses a window that ends before it starts, as the model does.
--
-- `created_at` is when the note first reached the corpus. The importer sets it from the git
-- history (`note_arrivals`) so a digest does not call the whole corpus new; a note written after
-- the flip takes the default. `updated_at` moves with each revision, so a cache can key on the
-- count and the newest value instead of reading every note.
CREATE TABLE IF NOT EXISTS kg_notes (
    tenant_id   TEXT        NOT NULL DEFAULT 'default',
    id          TEXT        NOT NULL,
    type        TEXT        NOT NULL,
    frontmatter JSONB       NOT NULL DEFAULT '{}'::jsonb,
    body        TEXT        NOT NULL DEFAULT '',
    created_by  TEXT        NOT NULL DEFAULT 'human',
    valid_from  DATE,
    valid_to    DATE,
    revision    INTEGER     NOT NULL DEFAULT 1,
    content_sha TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    validity    daterange   GENERATED ALWAYS AS (daterange(valid_from, valid_to, '[]')) STORED,
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT kg_notes_id_is_a_slug CHECK (
        id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$' AND id !~ '\.\.' AND id !~ '(\.|\.lock)$'
    ),
    CONSTRAINT kg_notes_type_is_a_slug CHECK (
        type ~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$' AND type !~ '\.\.' AND type !~ '(\.|\.lock)$'
    ),
    CONSTRAINT kg_notes_created_by_is_known CHECK (created_by IN ('human', 'agent')),
    CONSTRAINT kg_notes_revision_is_positive CHECK (revision >= 1)
);

-- Retrievers and the digest filter by type; every discovery read filters by currency.
CREATE INDEX IF NOT EXISTS kg_notes_type_idx ON kg_notes (tenant_id, type);
CREATE INDEX IF NOT EXISTS kg_notes_validity_idx ON kg_notes USING gist (validity);

-- Every revision a note has had, oldest first: what `git log` was. A row is written with the note
-- it describes and is never changed, so the runtime role gets INSERT and nothing else. `recorded_at`
-- is the database clock (transaction time); `valid_from`/`valid_to` are the facts' own (valid
-- time). "What did we believe on 1 March" is the latest revision recorded by then.
--
-- No foreign key cascade: a note's history must not go with a note. `recorded_by` is the person
-- on whose behalf the revision was written (the turn's or job's actor), `NULL` when no person
-- was bound; it is the audit field git's committer, always the service identity, never was.
CREATE TABLE IF NOT EXISTS kg_note_revisions (
    tenant_id   TEXT        NOT NULL DEFAULT 'default',
    note_id     TEXT        NOT NULL,
    revision    INTEGER     NOT NULL,
    type        TEXT        NOT NULL,
    frontmatter JSONB       NOT NULL,
    body        TEXT        NOT NULL,
    created_by  TEXT        NOT NULL,
    valid_from  DATE,
    valid_to    DATE,
    content_sha TEXT        NOT NULL,
    recorded_by TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, note_id, revision),
    FOREIGN KEY (tenant_id, note_id) REFERENCES kg_notes (tenant_id, id),
    CONSTRAINT kg_note_revisions_created_by_is_known CHECK (created_by IN ('human', 'agent')),
    CONSTRAINT kg_note_revisions_revision_is_positive CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS kg_note_revisions_recorded_idx ON kg_note_revisions (recorded_at);

-- The typed edges a note asserts, from its body `[[rel:target]]` links and its `relations:`
-- frontmatter together (`Note.outgoing_relations`), derived when the note is written. `dst` has no
-- foreign key: a link may name an id no note defines yet, or a `reaction-<id>` record that lives
-- in another table, and the graph has always kept those as bare nodes. `src` cascades because the
-- edges are a function of the note's current revision, not history.
--
-- The window and `confidence` are the edge's own (`kg.note.Relation`), separate from its source
-- note's: a relation can stop holding while both notes remain current.
CREATE TABLE IF NOT EXISTS kg_edges (
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    src        TEXT NOT NULL,
    rel        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    valid_from DATE,
    valid_to   DATE,
    confidence DOUBLE PRECISION,
    validity   daterange GENERATED ALWAYS AS (daterange(valid_from, valid_to, '[]')) STORED,
    PRIMARY KEY (tenant_id, src, rel, dst),
    FOREIGN KEY (tenant_id, src) REFERENCES kg_notes (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT kg_edges_confidence_is_a_probability CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    )
);

-- The primary key serves outgoing and one-relation lookups. Incoming edges (a retired note's
-- `supersedes` links, the second hop of a neighbourhood) and relation scans need their own.
CREATE INDEX IF NOT EXISTS kg_edges_dst_idx ON kg_edges (tenant_id, dst, rel);
CREATE INDEX IF NOT EXISTS kg_edges_rel_idx ON kg_edges (tenant_id, rel);

-- `note_index` follows the notes by key instead of by a re-read of the checkout. Both columns are
-- nullable and unwritten for now, so the previous image keeps writing rows without them; MATCH
-- FULL makes a row carry both or neither. `note_index` has no tenant of its own yet, so the key
-- names the note's tenant beside its id. The next release fills them from the imported notes; the
-- one after stops reading `fingerprint` and `corpus_commit_count`, which exist only because the
-- corpus used to be a per-pod checkout.
ALTER TABLE note_index ADD COLUMN IF NOT EXISTS kg_tenant_id TEXT;
ALTER TABLE note_index ADD COLUMN IF NOT EXISTS kg_note_id TEXT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'note_index'::regclass AND conname = 'note_index_kg_note_fkey'
    ) THEN
        ALTER TABLE note_index
            ADD CONSTRAINT note_index_kg_note_fkey FOREIGN KEY (kg_tenant_id, kg_note_id)
            REFERENCES kg_notes (tenant_id, id) MATCH FULL ON DELETE CASCADE;
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS note_index_kg_note_idx ON note_index (kg_tenant_id, kg_note_id);
