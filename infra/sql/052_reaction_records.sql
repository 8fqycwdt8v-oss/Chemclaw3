-- ELN reaction records: the transcription tier (D-2026-08-25).
--
-- Every ELN entry used to be rendered as a `created_by: agent` markdown note and pushed through
-- the PR-gate for a human to merge. D-005's gate exists to put a human in front of *machine-
-- generated knowledge*, and a transcription is not that: `record_from_ord_reaction` is a pure
-- deterministic mapping with no model in it, so the reviewer was approving a rendering of data a
-- chemist had already signed off on upstream. Measured, the gate cost 202 ms of serialized git per
-- entry and a corpus scan that outgrows `eln_sync_timeout_seconds` at ~700k notes, and it bought
-- nothing a reviewer could decide.
--
-- **Postgres, not Git — the same argument migration `025` makes for observations.** Git's value is
-- human review, diff and audit. With no review it buys a branch per entry and a repo that cannot
-- hold a real ELN, and returns nothing. This *preserves* "git is the source of truth" precisely
-- because a transcription is not a knowledge claim: what a human asserts about these runs still
-- lives in `knowledge/` as a playbook or a campaign, gated as it always was, citing these records.
--
-- The columns are exactly what the three readers need and nothing more: the rendered body for
-- `expand_note`, and the four metadata fields `retrieval.retrievers._NOTE_FILTERS` narrows on.
CREATE TABLE IF NOT EXISTS reaction_records (
    -- The ELN's own reaction id. The note id every citation uses is `reaction-<reaction_id>`
    -- (`kg.note.note_id_for_reaction`); the prefix is not stored, so a lookup never has to strip it
    -- and two spellings of one record cannot exist.
    reaction_id      TEXT        PRIMARY KEY,
    -- The rendered transcription: conditions, charge sheet, impurity profile, procedure. This is
    -- what a chemist reads when a structure search hits and they expand the record.
    body             TEXT        NOT NULL,
    -- The molecule this record is *about*, when the entry names exactly one outcome — the same
    -- rule and the same `NULL` as before, because a reaction reporting a product and two
    -- by-products has no honest answer and a wrong one is worse than none.
    compound_smiles  TEXT,
    -- The entry's project: the one grouping key an ELN already carries, and what `tag=` filters on.
    project          TEXT,
    -- When the experiment was run. `since`/`until` window on this, so a record with no date is
    -- outside every window rather than inside all of them.
    performed_at     DATE,
    source           TEXT        NOT NULL,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The eligibility filter reads project and date together (`tag` + `since`/`until` may be combined),
-- and always over the id set a fingerprint search just returned — so the PK carries the lookup and
-- this carries the narrowing.
CREATE INDEX IF NOT EXISTS reaction_records_filter_idx
    ON reaction_records (project, performed_at);
