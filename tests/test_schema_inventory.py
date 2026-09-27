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

from tests.test_migrations_are_additive import (
    _REVIEWED_REPLAY_BREAKS,
    _REVIEWED_ROLLBACK_BREAKS,
    _REVIEWED_SEMANTIC_BREAKS,
)

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

# The two operator lists at the foot of the README, each keyed by its own `###` heading rather than
# by position, and each read as the migration filenames its bullets open with. Scoped to a heading
# on purpose: migration filenames appear in the README's running prose too (`037_document_index.sql`
# is named in the "two files may share a number" paragraph), so a whole-file scan would read that as
# a claim about rollbacks.
_ROLLBACK_HEADING = '### Migrations that end "deploy the previous image"'
_REPLAY_HEADING = "### Migrations that are not re-runnable, and the recipe for each"
_BULLET = re.compile(r"^- `(\d{3}_[a-z0-9_]+\.sql)`", re.MULTILINE)


def _listed_under(heading: str) -> list[str]:
    """The migration filenames the bullets under `heading` name, in the order they are listed."""
    body = _README.read_text(encoding="utf-8")
    assert heading in body, f"infra/sql/README.md no longer has the section {heading!r}"
    after = body.split(heading, 1)[1]
    return _BULLET.findall(after.split("\n### ", 1)[0].split("\n## ", 1)[0])


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
    # An anonymous block, credited to the table its first `ALTER TABLE` acts on — the shape `108`
    # has, a guarded `ADD CONSTRAINT`. A block that altered no table would match nothing here and
    # fail `test_every_migration_statement_is_one_the_rule_understands` rather than pass unread.
    re.compile(rf"^DO\s+\$\$.*?\bALTER TABLE\s+(?:IF EXISTS\s+)?(?:ONLY\s+)?({_NAME})", re.I),
)
# Statements that legitimately name no table.
#
# `DROP INDEX` is here rather than in `_TOUCHES` because its syntax names an *index*, never the
# table under it — so there is no table to credit, and crediting the index's own name to the
# Migration column would put a non-table in it. It is a real construct in this directory since
# `106`, which drops an index `105` created for a containment query nobody ever wrote.
_TABLE_FREE = (
    re.compile(r"^CREATE EXTENSION", re.I),
    re.compile(r"^DROP INDEX", re.I),
)


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

    **A `DO $$ … $$` block is one statement too**, and for the same reason: its body is PL/pgSQL
    with semicolons of its own, which the runner sends whole (`core.migrate` says so) and a split
    here would turn into `END IF` and `END $$` fragments naming nothing. The first one is `108`, a
    constraint added behind a `pg_constraint` guard because `ALTER TABLE … ADD CONSTRAINT` has no
    `IF NOT EXISTS`. Only the anonymous `$$` tag is read, because it is the only one this directory
    writes; a quote inside the body does not toggle the literal state, since the body is itself the
    literal.
    """
    out: list[str] = []
    current: list[str] = []
    in_literal = False
    in_dollar = False
    for index, char in enumerate(body):
        # The opening `$` of a `$$` pair toggles; its second `$` is not itself the start of one.
        if not in_literal and body.startswith("$$", index) and body[index - 1 : index] != "$":
            in_dollar = not in_dollar
        if char == "'" and not in_dollar:
            in_literal = not in_literal
        if char == ";" and not in_literal and not in_dollar:
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


def test_a_do_block_is_one_statement_and_names_its_table() -> None:
    """A PL/pgSQL body's own semicolons do not end the statement the runner sends whole.

    Driven on synthetic SQL for the reason the comment test above is: the tree's one block could be
    rewritten tomorrow without a semicolon inside it, and this would then pass for nothing.
    """
    body = (
        "DO $$\nBEGIN\n    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'c') THEN\n"
        "        ALTER TABLE ONLY public.audit_events ADD CONSTRAINT c CHECK (a <> ';');\n"
        "    END IF;\nEND\n$$;\nCREATE INDEX IF NOT EXISTS i ON t (a);\n"
    )
    statements = [" ".join(raw.split()) for raw in _split_on_statement_ends(body)]
    statements = [s for s in statements if s]
    assert len(statements) == 2
    assert table_named_by(statements[0]) == "audit_events"
    assert table_named_by(statements[1]) == "t"


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


def test_the_rollback_note_names_every_reviewed_break() -> None:
    """The list an operator reads before a `helm rollback`, checked against the registers.

    It was transcribed, and it was wrong in the direction that matters: the README said **four**
    reviewed rollback-breaking migrations and listed 041, 056, 058, 063 while the register held
    five, the missing one being 088 — the newest, and the only one bearing on a rollback of the
    current release. So a paragraph whose own sentence claimed the list was "derived from that set"
    told an operator that the `turn_costs` primary-key move is not a rollback break. It is.

    Checked in both directions and in order, the same shape as the **Migration** column above: a
    register entry with no bullet is a break nobody planning a rollback will see, and a bullet with
    no entry is a warning about a migration that does not break anything. The count that used to
    open the paragraph is gone rather than checked — it is derivable from the list, and a redundant
    number is the thing that went stale.

    Both registers, because an operator does not care which one found the break: one holds the
    migrations a pattern flagged, the other the one that only review could reach.
    """
    reviewed = sorted(set(_REVIEWED_ROLLBACK_BREAKS) | set(_REVIEWED_SEMANTIC_BREAKS))
    assert _listed_under(_ROLLBACK_HEADING) == reviewed, (
        "infra/sql/README.md's rollback list disagrees with `_REVIEWED_ROLLBACK_BREAKS` + "
        f"`_REVIEWED_SEMANTIC_BREAKS` (which say {reviewed}). Extend the list in the same commit "
        "as the exemption — an operator plans a rollback from this file, not from a test"
    )


def test_the_replay_note_names_every_recipe() -> None:
    """The same, for the migrations that cannot simply be replayed.

    A restore whose `schema_migrations` ledger is older than its tables is recovered by re-running
    the migrations, and two of them abort the run instead (046 on a restore, 058 on a
    hand-built database). The recipe for each is one statement, and it is useless in a test file:
    the person who needs it is reading this directory at the time.
    """
    assert _listed_under(_REPLAY_HEADING) == sorted(_REVIEWED_REPLAY_BREAKS), (
        "infra/sql/README.md's replay-recipe list disagrees with `_REVIEWED_REPLAY_BREAKS` "
        f"(which says {sorted(_REVIEWED_REPLAY_BREAKS)})"
    )
    body = _README.read_text(encoding="utf-8")
    for name, (_, _, recipe) in _REVIEWED_REPLAY_BREAKS.items():
        assert recipe in body, (
            f"{name}'s replay recipe is not in infra/sql/README.md verbatim: {recipe!r}. A recipe "
            "an operator has to reconstruct is a recipe nobody runs under pressure"
        )


# The notations a second structure-identity scheme would arrive as. Names, not prose: `051`'s own
# comment lists three of these while declining them, and a test that matched the comment would
# pass on a migration that adds the column beside it
# (`D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability`).
_SECOND_IDENTITY = re.compile(
    r"^(std_)?(inchi|inchi_key|inchikey|cas|cas_number|cas_rn|formula|molecular_formula"
    r"|molecular_weight|mol_weight|registry_number|corporate_id)$"
)

_ADD_COLUMN = re.compile(rf"ADD COLUMN(?: IF NOT EXISTS)?\s+({_NAME})", re.I)
_CREATE_BODY = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+{_NAME}\s*\((.*)\)", re.I)


def _top_level_parts(body: str) -> list[str]:
    """Split a `CREATE TABLE` body on the commas that separate its definitions.

    Depth-aware, because `VARCHAR(64)` and a `CHECK (a IN ('x', 'y'))` both carry commas that do
    not separate anything.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _declared_columns() -> list[tuple[str, str]]:
    """Every `(file, column)` this family's schemas declare — both databases.

    `schema/result-store/` is included because it is the *other* place a compound is named, and the
    argument being held is about structure identity across both: the result store's `compound` row
    reuses `compound_id` deliberately, and a second scheme added there would be exactly as dead as
    one added here.
    """
    out: list[tuple[str, str]] = []
    files = sorted(_SQL.glob("*.sql")) + sorted((_ROOT / "schema").rglob("*.sql"))
    for path in files:
        body = _LINE_COMMENT.sub(" ", path.read_text(encoding="utf-8"))
        for raw in _split_on_statement_ends(body):
            statement = " ".join(raw.split())
            if (match := _CREATE_BODY.search(statement)) is not None:
                for part in _top_level_parts(match.group(1)):
                    words = part.split()
                    if words and words[0].upper() not in {
                        "PRIMARY",
                        "FOREIGN",
                        "UNIQUE",
                        "CHECK",
                        "CONSTRAINT",
                        "EXCLUDE",
                    }:
                        out.append((path.name, _bare(words[0])))
            for added in _ADD_COLUMN.findall(statement):
                out.append((path.name, _bare(added)))
    return out


def test_the_schemas_declare_columns_at_all() -> None:
    """The positive control: a scan that found nothing would pass the guard below forever."""
    columns = _declared_columns()
    assert len(columns) > 100, f"only {len(columns)} column(s) parsed; the scan stopped working"
    assert ("052_reaction_records.sql", "reaction_id") in columns
    assert ("001_core.sql", "canonical_smiles") in columns


def test_no_schema_mints_a_second_structure_identity() -> None:
    """Structure identity is the standardized SMILES and nothing else.

    `051_reaction_labels.sql` declined an InChIKey, a formula and a molecular weight because
    nothing asked and this tree deletes dead columns, and
    `D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability` measured the
    argument that was supposed to reopen it — that an InChIKey survives a `STANDARDIZATION_VERSION`
    bump — and found it false: an InChIKey taken after standardization moves exactly when
    `compound_id` moves, and one taken before it fragments the join `standard_smiles` exists to
    make. So a column here is dead on the day it is added, in both databases.

    This is a column-name check over comment-stripped SQL, which is the whole reason it can fail:
    the three notations it forbids are named in `051`'s own prose, so a test reading the file as
    text would pass on a migration that adds the column directly beneath that sentence.
    """
    minted = sorted(
        {
            f"{path}:{column}"
            for path, column in _declared_columns()
            if _SECOND_IDENTITY.match(column)
        }
    )
    assert not minted, (
        f"{minted} declares a second structure identity. The honest form of that change is to "
        "name the reader first (D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-"
        "instability), and a site's own registry number rides `Component.attributes` today with "
        "no schema change at all"
    )
