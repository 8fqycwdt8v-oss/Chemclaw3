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

Two of the four columns are judgements and stay unverified: **Written by** names the module that
owns a table's writes and **Disposal** says what bounds its growth, and a test for either would be
a second copy of the answer or a regex over English.

**Migration is not a judgement.** Which files touch a table is a fact the files state, so the
column that says so is checkable — and it was the one part of an inventory whose own prose
advertises being verified that nothing verified. Measured on the shipped set, four of twenty-seven
rows were wrong: `bo_suggestions` omitted 037, `calculation_results` omitted 019,
`note_proposals` omitted 036, `session_messages` omitted 026 — every one of them a migration that
had added a column the row did not mention. So the set check below is joined by a column check,
and the rule is kept honest by refusing to pass over a statement shape it does not understand.
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
# The same row, keeping the second cell: the Migration column.
_MIGRATION_CELL = re.compile(r"^\|\s*`(\w+)`\s*\|([^|]*)\|", re.MULTILINE)
_NUMBER = re.compile(r"\d{3}")

_LINE_COMMENT = re.compile(r"--[^\n]*")

# A statement acts on the table it names in one of these positions. Matching the construct rather
# than the bare identifier is load-bearing: `observations` is both a table and a column of
# `bo_suggestions`, so "the name appears in the file" would credit migration 031 with touching a
# table it only mentions as a column.
_TOUCHES = (
    re.compile(r"^CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", re.I),
    re.compile(r"^ALTER TABLE(?:\s+IF EXISTS)?\s+(\w+)", re.I),
    re.compile(
        r"^CREATE(?:\s+UNIQUE)?\s+INDEX(?:\s+CONCURRENTLY)?"
        r"(?:\s+IF NOT EXISTS)?\s+\w+\s+ON\s+(\w+)",
        re.I,
    ),
    re.compile(r"^COMMENT ON TABLE\s+(\w+)", re.I),
    re.compile(r"^COMMENT ON COLUMN\s+(\w+)\.", re.I),
    re.compile(r"^INSERT INTO\s+(\w+)", re.I),
    re.compile(r"^UPDATE\s+(\w+)\s", re.I),
)
# Statements that legitimately name no table.
_TABLE_FREE = (re.compile(r"^CREATE EXTENSION", re.I),)


def _statements() -> list[tuple[str, str, str]]:
    """Every migration statement as `(file name, migration number, normalised SQL)`.

    Line comments are stripped first — half the prose in this directory names tables it does not
    touch, including four rows' worth of "migration 027 justifies ..." back-references.
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(_SQL.glob("*.sql")):
        body = _LINE_COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        for raw in body.split(";"):
            statement = " ".join(raw.split())
            if statement:
                out.append((path.name, path.name.split("_")[0], statement))
    return out


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


def migrations_that_touch_each_table() -> dict[str, list[str]]:
    """Table -> the migration numbers whose statements act on it, in application order.

    A number appears once however many of its statements touch the table, and two files sharing a
    number (037 does) contribute that number once — which is what the README's column means.
    """
    tables = tables_on_disk()
    touched: dict[str, list[str]] = {table: [] for table in tables}
    for _, number, statement in _statements():
        for pattern in _TOUCHES:
            match = pattern.match(statement)
            if match is None:
                continue
            table = match.group(1).lower()
            if table in tables and number not in touched[table]:
                touched[table].append(number)
    return touched


def migrations_in_the_inventory() -> dict[str, list[str]]:
    """Table -> the migration numbers its README row claims, in the order the cell lists them."""
    return {
        table.lower(): _NUMBER.findall(cell)
        for table, cell in _MIGRATION_CELL.findall(_README.read_text(encoding="utf-8"))
    }


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


def test_every_migration_statement_is_one_the_rule_understands() -> None:
    """Guard the guard: an unrecognised statement must fail loudly, not count as touching nothing.

    Without this, teaching the schema a construct `_TOUCHES` does not list — a `COMMENT ON`-only
    migration, a `DELETE FROM` backfill — would silently stop crediting that migration to its
    table, and the column check below would pass while going stale in exactly the way it exists to
    prevent. Failing here costs one regex; the alternative is a test that quietly stops testing.
    """
    unrecognised = [
        f"{name}: {statement[:60]}"
        for name, _, statement in _statements()
        if not any(pattern.match(statement) for pattern in _TOUCHES + _TABLE_FREE)
    ]
    assert not unrecognised, (
        "infra/sql statements tests/test_schema_inventory.py cannot classify: "
        f"{unrecognised}. Add the construct to _TOUCHES (it names a table) or to _TABLE_FREE (it "
        "does not), so the Migration column keeps being checked"
    )


def test_the_migration_column_names_every_migration_that_touches_the_table() -> None:
    """The column the README's "an inventory nobody verifies" paragraph vouched for.

    It was the one column nothing checked, and four of twenty-seven rows were wrong — each of them
    a later `ALTER TABLE` adding a column the row never mentioned. A reader using this table to
    answer "when did this table last change shape" got the wrong answer for a seventh of it.
    """
    actual = migrations_that_touch_each_table()
    declared = migrations_in_the_inventory()
    wrong = {
        table: (declared.get(table, []), numbers)
        for table, numbers in sorted(actual.items())
        if declared.get(table, []) != numbers
    }
    assert not wrong, (
        "infra/sql/README.md's Migration column disagrees with the migrations, "
        f"{{table: (row says, files say)}}: {wrong}. Extend the row in the same commit as the "
        "migration — a later ALTER TABLE belongs in the cell as much as the CREATE does"
    )
