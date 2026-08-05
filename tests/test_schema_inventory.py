"""`infra/sql/README.md` lists exactly the tables the migrations create.

The repository had no current inventory of its own schema. The only one that existed sits in
`docs/archive/audit/13-storage-and-knowledge-audit.md`, describes "nineteen files in `infra/sql/`"
against the thirty-six that shipped, and stops at migration 019 — so the single document a reader
would reach for was seventeen migrations out of date, under a directory `docs/README.md` marks "do
not treat any of these as current".

Writing a fresh one only helps if something keeps it true. This is that something, and it is the
same bidirectional shape `tests/test_repo_map.py` uses on `ARCHITECTURE.md`: a table on disk with
no row is an undocumented table, and a row with no table is a document describing something that
does not exist. Both directions matter — the second is how the archived inventory decayed, one
renamed migration at a time.

Deliberately structural. It checks the *set* of tables, not the prose in the other columns: who
writes a table and what disposes of it are judgements, and a test that tried to verify them would
either be a second copy of the answer or a regex over English.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SQL = _ROOT / "infra" / "sql"
_README = _SQL / "README.md"

_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.I)
# A row's first cell, which is the table name in backticks. Anchored to the line start so the
# "Two things the shape of this table will not tell you" prose below — which mentions
# `calculation_artifacts` and `bo_suggestions` in running text — cannot be mistaken for rows.
_ROW = re.compile(r"^\|\s*`(\w+)`\s*\|", re.MULTILINE)


def tables_on_disk() -> set[str]:
    """Every table the migration set creates."""
    return {
        match.lower()
        for path in sorted(_SQL.glob("*.sql"))
        for match in _CREATE.findall(path.read_text(encoding="utf-8"))
    }


def tables_in_the_inventory() -> set[str]:
    """Every table `infra/sql/README.md` has a row for."""
    return {name.lower() for name in _ROW.findall(_README.read_text(encoding="utf-8"))}


def test_there_are_tables_to_inventory() -> None:
    """Guard the guard: an empty glob would make every assertion below vacuously true.

    This repository has hit the vacuous-pass shape repeatedly — most recently the migration reader
    itself, which globbed a directory inside the package and applied zero files without failing.
    """
    assert len(tables_on_disk()) > 20


def test_every_table_has_a_row_in_the_inventory() -> None:
    """A table nobody documented is a table whose disposal story nobody stated either."""
    missing = tables_on_disk() - tables_in_the_inventory()
    assert not missing, (
        f"tables created by a migration with no row in infra/sql/README.md: {sorted(missing)}. "
        "Add the row in the same commit as the migration — including what bounds its growth, or "
        "that nothing does"
    )


def test_the_inventory_lists_no_table_that_does_not_exist() -> None:
    """The direction the archived inventory decayed in: rows outliving what they describe."""
    phantom = tables_in_the_inventory() - tables_on_disk()
    assert not phantom, (
        f"infra/sql/README.md documents tables no migration creates: {sorted(phantom)}"
    )
