-- Artefacts gain the `html` kind: a page the model wrote, rendered only in the UI's separate
-- sandbox origin and never served as `text/html` by this system
-- (D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves).
--
-- **A widening, written as a drop and a re-add because a CHECK has no other spelling** — the shape
-- `116_exhibit_geometry_kind.sql` took for the same reason. The previous image writes only the seven
-- kinds it knows, every one of which the new constraint still admits, so its writes keep working and
-- "deploy the previous image" stays the rollback for writes. What it cannot do is *read* an html
-- revision: its `parse_spec` refuses the kind, so the listing still serves the header while opening
-- or exporting that artefact fails. The same image also cannot read a revision that *binds* a value
-- (`D-2026-10-03-an-artefact-binds-a-value-to-the-result-it-came-from`), which needs no schema
-- change — a binding is JSON inside the existing `spec` column. `tests/test_migrations_are_additive.py`
-- holds the exemption to exactly the one statement it covers.
--
-- Dropped `IF EXISTS`, so a replay against a restore that already carries this constraint re-adds
-- the same widened form either way.
--
-- Applied by `make db-migrate`.
ALTER TABLE session_exhibits DROP CONSTRAINT IF EXISTS session_exhibits_kind_known;
ALTER TABLE session_exhibits
    ADD CONSTRAINT session_exhibits_kind_known
        CHECK (kind IN (
            'document', 'table', 'structures', 'chart', 'result', 'link', 'geometry', 'html'
        ));
