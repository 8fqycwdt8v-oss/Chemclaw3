-- Artefacts gain the `geometry` kind: one 3D structure, inline XYZ or a cited calculation artifact
-- (D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy).
--
-- **A widening, written as a drop and a re-add because a CHECK has no other spelling.** The
-- previous image writes only the six kinds it knows, every one of which the new constraint still
-- admits, so its writes keep working and "deploy the previous image" stays the rollback for writes.
-- What it cannot do is *read* a geometry revision: its `parse_spec` refuses the kind, so the
-- listing still serves the header while opening or exporting that artefact fails. The ADR and the
-- runbook's rollback table say so; `tests/test_migrations_are_additive.py` holds the exemption to
-- exactly the one statement it covers.
--
-- Dropped `IF EXISTS`, so a replay against a restore that already carries this constraint, or a
-- database built without it, re-adds the same widened form either way.
--
-- Applied by `make db-migrate`.
ALTER TABLE session_exhibits DROP CONSTRAINT IF EXISTS session_exhibits_kind_known;
ALTER TABLE session_exhibits
    ADD CONSTRAINT session_exhibits_kind_known
        CHECK (kind IN ('document', 'table', 'structures', 'chart', 'result', 'link', 'geometry'));
