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

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SQL = _ROOT / "infra" / "sql"
_README = _SQL / "README.md"

# How a table may be spelled where a statement names one. Every pattern below used to say `\w+`,
# which is the bare lower-case spelling every merged migration happens to use and only that one —
# so `ALTER TABLE ONLY audit_events …`, the form **`pg_dump` emits**, resolved to the "table"
# `only`, and `public.audit_events` to `public`. Neither is in the inventory, so the migration was
# credited to no table at all and the column check below passed over the row it had just stopped
# checking. Written once, substituted everywhere, and normalised by `_bare` so the schema qualifier
# and the quotes are dropped rather than compared.
_NAME = r"[\w.\"]+"


def _bare(identifier: str) -> str:
    """`public."audit_events"` -> `audit_events`: the identifier without schema or quotes."""
    return identifier.replace('"', "").rsplit(".", 1)[-1].lower()


_CREATE = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+({_NAME})", re.I)
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
    re.compile(rf"^CREATE TABLE(?:\s+IF NOT EXISTS)?\s+({_NAME})", re.I),
    re.compile(rf"^ALTER TABLE\s+(?:IF EXISTS\s+)?(?:ONLY\s+)?({_NAME})", re.I),
    re.compile(
        r"^CREATE(?:\s+UNIQUE)?\s+INDEX(?:\s+CONCURRENTLY)?"
        rf"(?:\s+IF NOT EXISTS)?\s+{_NAME}\s+ON\s+(?:ONLY\s+)?({_NAME})",
        re.I,
    ),
    re.compile(rf"^COMMENT ON TABLE\s+({_NAME})", re.I),
    re.compile(rf"^COMMENT ON COLUMN\s+({_NAME})\.", re.I),
    re.compile(rf"^INSERT INTO\s+({_NAME})", re.I),
    re.compile(rf"^UPDATE\s+({_NAME})\s", re.I),
)
# Statements that legitimately name no table.
_TABLE_FREE = (re.compile(r"^CREATE EXTENSION", re.I),)


def _split_on_statement_ends(body: str) -> list[str]:
    """Split SQL on the semicolons that end a statement, ignoring those inside a string literal.

    A plain `body.split(";")` tears any statement whose *prose* contains a semicolon into
    fragments — and the one construct in this directory that carries prose is `COMMENT ON`, whose
    whole purpose is to explain a column in sentences. Two migrations wrote one ("... could
    resolve; a sourced write supersedes ...", "... withdrawn; NULL means not retracted"), and each
    fragment then matched no pattern at all.

    That failed loudly rather than silently, because `test_every_migration_statement_is_one_the
    _rule_understands` exists — but the failure it reported named the migrations, not this
    function, which is why the fix belongs here rather than in a new `_TOUCHES` entry: the
    construct was already listed, and the text was never one statement to begin with.

    SQL escapes a quote inside a literal by doubling it, and a doubled quote is just two state
    flips in a row, so tracking a single boolean is sufficient and `''` needs no special case.
    """
    out: list[str] = []
    current: list[str] = []
    in_literal = False
    for char in body:
        if char == "'":
            in_literal = not in_literal
        if char == ";" and not in_literal:
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    out.append("".join(current))
    return out


def _statements() -> list[tuple[str, str, str]]:
    """Every migration statement as `(file name, migration number, normalised SQL)`.

    Line comments are stripped first — half the prose in this directory names tables it does not
    touch, including four rows' worth of "migration 027 justifies ..." back-references.
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(_SQL.glob("*.sql")):
        body = _LINE_COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        for raw in _split_on_statement_ends(body):
            statement = " ".join(raw.split())
            if statement:
                out.append((path.name, path.name.split("_")[0], statement))
    return out


def tables_on_disk() -> set[str]:
    """Every table the migration set creates."""
    return {
        _bare(match)
        for path in sorted(_SQL.glob("*.sql"))
        for match in _CREATE.findall(path.read_text(encoding="utf-8"))
    }


def tables_in_the_inventory() -> set[str]:
    """Every table `infra/sql/README.md` has a row for."""
    return {name.lower() for name in _ROW.findall(_README.read_text(encoding="utf-8"))}


def table_named_by(statement: str) -> str | None:
    """The table a statement acts on, or `None` for a statement no `_TOUCHES` construct matches."""
    for pattern in _TOUCHES:
        match = pattern.match(statement)
        if match is not None:
            return _bare(match.group(1))
    return None


def migrations_that_touch_each_table() -> dict[str, list[str]]:
    """Table -> the migration numbers whose statements act on it, in application order.

    A number appears once however many of its statements touch the table, and two files sharing a
    number (037 does) contribute that number once — which is what the README's column means.
    """
    tables = tables_on_disk()
    touched: dict[str, list[str]] = {table: [] for table in tables}
    for _, number, statement in _statements():
        table = table_named_by(statement)
        if table is None or table not in tables:
            continue
        if number not in touched[table]:
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


def test_a_semicolon_inside_a_comment_does_not_end_the_statement() -> None:
    """The splitter must read SQL, not text that mostly looks like SQL.

    `COMMENT ON` is the one construct here that carries sentences, so it is the one that will
    contain a semicolon, an apostrophe, or both. Splitting naively turned one such comment into
    two fragments that named no table — which would have stopped crediting its migration to its
    table, the exact decay the surrounding tests exist to catch.

    Driven directly rather than through the corpus: a migration that happens to contain no
    semicolon in its prose today would make this pass for the wrong reason tomorrow.
    """
    body = (
        "ALTER TABLE t ADD COLUMN c TEXT;\n"
        "COMMENT ON COLUMN t.c IS 'one; two, and the site''s own third';\n"
        "CREATE INDEX t_c_idx ON t (c);\n"
    )
    statements = [" ".join(raw.split()) for raw in _split_on_statement_ends(body)]
    assert [s for s in statements if s] == [
        "ALTER TABLE t ADD COLUMN c TEXT",
        "COMMENT ON COLUMN t.c IS 'one; two, and the site''s own third'",
        "CREATE INDEX t_c_idx ON t (c)",
    ]


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


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE IF NOT EXISTS audit_events (a TEXT)",
        "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS c TEXT",
        "ALTER TABLE public.audit_events ADD COLUMN IF NOT EXISTS c TEXT",
        "ALTER TABLE ONLY audit_events ADD COLUMN IF NOT EXISTS c TEXT",
        "ALTER TABLE IF EXISTS audit_events ADD COLUMN IF NOT EXISTS c TEXT",
        'ALTER TABLE "audit_events" ADD COLUMN IF NOT EXISTS c TEXT',
        'ALTER TABLE public."audit_events" ADD COLUMN IF NOT EXISTS c TEXT',
        "CREATE INDEX IF NOT EXISTS i ON audit_events (a)",
        "CREATE INDEX IF NOT EXISTS i ON ONLY public.audit_events (a)",
        "COMMENT ON TABLE public.audit_events IS 'x'",
        "COMMENT ON COLUMN public.audit_events.a IS 'x'",
        "INSERT INTO public.audit_events (a) VALUES ('x')",
        "UPDATE public.audit_events SET a = 'x'",
    ],
)
def test_a_table_is_recognised_however_it_is_spelled(statement: str) -> None:
    """Every spelling Postgres accepts names the same table — or the column check goes blind.

    The failure this closes is silent, which is why it is asked of synthetic SQL rather than of the
    tree. `ALTER TABLE ONLY audit_events …` is the form **`pg_dump` emits**; read by a rule that
    expects a bare identifier it yields the "table" `only`, which is in no inventory, so the
    migration is credited to nothing and `test_the_migration_column_names_every_migration_that_
    touches_the_table` below passes over a row it has just stopped checking. A schema qualifier
    resolves to `public` the same way. Every merged migration happens to use the bare lower-case
    spelling, so the tree can never raise this — only these rows can.
    """
    assert table_named_by(statement) == "audit_events"


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
