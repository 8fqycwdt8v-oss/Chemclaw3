"""The grant matrix is derived from the code, not maintained beside it.

`infra/sql/grants/app_privileges.sql` says what the runtime role may write. This derives the same
answer from the SQL literals in `src/` and fails if the two disagree in **either** direction
(D-2026-08-05-append-only-by-grant-not-by-contract):

- A verb the code uses and the grant withholds is an outage — the application hits
  `InsufficientPrivilege` on a path nobody exercised before the deploy.
- A verb the grant allows and the code never uses is the boundary quietly widening back out. That
  direction is the one this file exists for: `audit_events` was called "append-only by contract"
  for a year while nothing enforced it, and a grant that drifts is how the contract stops being
  true again without anyone editing the sentence that claims it.

This is the same shape as `connector-validate` and `datasource-validate`: a declaration checked
against the live surface rather than a second definition of it. It needs no database — the check is
between two files in the repository, which is what makes it run in every environment.
"""

import ast
import re
from pathlib import Path

from chemclaw.durable.retention import _PRUNABLE

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "chemclaw"
_SQL = _ROOT / "infra" / "sql"
_GRANTS = _SQL / "grants" / "app_privileges.sql"

# Anything that reads like SQL. Docstrings and comments are excluded by construction: `ast` only
# yields string *constants*, and the SQL filter drops the prose ones — which matters, because this
# repository's docstrings discuss `DELETE` and `UPDATE` at length.
_LOOKS_LIKE_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|TRUNCATE)\b", re.I)

_INSERT = re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.I)
_UPDATE = re.compile(r"\bUPDATE\s+(\w+)\s+SET", re.I)
_DELETE = re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.I)
_UPSERT = re.compile(r"\bINSERT\s+INTO\s+(\w+).*?\bON CONFLICT\b.*?\bDO UPDATE\b", re.I)

# The two places a table name reaches a statement through a variable rather than a literal, so no
# scan of the SQL text can see them. Named here with their authority rather than hardcoded, so each
# stays true if its source changes.
#
# - The fingerprint stores build `INSERT INTO {table} ... ON CONFLICT DO UPDATE` in `__init__`
#   (`science/fingerprints/store.py`), with the table from `default_molecule_store` /
#   `default_reaction_store`.
# - The retention sweep builds `DELETE FROM {table}` over the closed `_PRUNABLE` map.
#
# - The LangGraph checkpointer and store issue their own SQL from inside the installed package, so
#   *no* first-party literal names them at all. Their verbs are recorded here, read off those
#   packages rather than assumed: `checkpoints`/`checkpoint_writes`/`store`/`store_vectors` upsert
#   with `ON CONFLICT … DO UPDATE`, `checkpoint_blobs` uses `DO NOTHING` and so needs no UPDATE, and
#   the three version ledgers take one INSERT per schema step. The DELETEs on the checkpoint tables
#   and on `store`/`store_vectors` are ours (retention by thread, erasure by subject) and *are*
#   visible as literals — they are folded in below by the ordinary scan.
_DYNAMIC: dict[str, set[str]] = {
    "molecule_fingerprints": {"INSERT", "UPDATE"},
    "reaction_fingerprints": {"INSERT", "UPDATE"},
    # `_PRUNABLE` first, so the fuller upstream matrix below wins for `checkpoints` rather than
    # being flattened back to the retention sweep's single DELETE — which is what a later `**`
    # expansion did, and it read as "the grant allows an INSERT nobody performs".
    **{table: {"DELETE"} for table in _PRUNABLE},
    "checkpoints": {"INSERT", "UPDATE", "DELETE"},
    "checkpoint_writes": {"INSERT", "UPDATE", "DELETE"},
    "checkpoint_blobs": {"INSERT", "DELETE"},
    "checkpoint_migrations": {"INSERT"},
    "store": {"INSERT", "UPDATE", "DELETE"},
    "store_vectors": {"INSERT", "UPDATE", "DELETE"},
    "store_migrations": {"INSERT"},
    "vector_migrations": {"INSERT"},
}

# Written by the migrator alone: the ledger of its own work. A runtime credential that could write
# it could mark a migration applied that never ran, so `migrate.py`'s INSERT is deliberately not
# part of the application's matrix.
_MIGRATOR_ONLY = {"schema_migrations"}


def _upstream_tables() -> set[str]:
    """Every table LangGraph's `setup()` creates, derived from the installed distributions.

    These exist in the same database and are declared by no file in `infra/sql`, because the
    checkpointer and the store build their own schema lazily on first use. Derived rather than
    listed for the reason the rest of this module is derived: a table upstream adds in a minor bump
    must fail the grant check, not inherit `GRANT SELECT` and be discovered as a write outage.

    The two version ledgers the store writes are named here instead of parsed. Upstream spells them
    inline in `setup()` (`_get_version(cur, table="store_migrations")`) rather than in the
    `MIGRATIONS` lists, so there is no statement to read them out of — `tests/test_upstream_surface`
    pins the names so a rename turns red here rather than silently un-granting them.
    """
    from langgraph.checkpoint.postgres import base as checkpoint_base
    from langgraph.store.postgres import base as store_base

    created = {"store_migrations", "vector_migrations"}
    for statements in (
        checkpoint_base.MIGRATIONS,
        store_base.MIGRATIONS,
        store_base.VECTOR_MIGRATIONS,
    ):
        for statement in statements:
            if match := re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", str(statement), re.I):
                created.add(match.group(1).lower())
    return created


def _tables() -> set[str]:
    """Every table this database holds: the migrations' and LangGraph's alike.

    The upstream half used to be absent, and its absence was not cosmetic. `note()` below drops any
    table it does not recognise, so `_DYNAMIC`'s entry naming `checkpoints` was discarded before it
    could assert anything and this file reported "the code writes what the grant withholds: {}"
    while the grant withheld every write on five tables.
    """
    names: set[str] = set()
    for path in sorted(_SQL.glob("*.sql")):
        names |= {
            match.lower()
            for match in re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+(\w+)", path.read_text(encoding="utf-8"), re.I
            )
        }
    return names | _upstream_tables()


def _joined(node: ast.JoinedStr) -> str:
    """An f-string's literal parts, with each interpolation standing in as a placeholder.

    Needed because a statement containing **one** interpolation is a `JoinedStr`, and walking for
    `ast.Constant` alone sees its literal pieces as separate strings — which splits `INSERT INTO x`
    away from its `ON CONFLICT ... DO UPDATE` and silently loses the UPDATE the upsert requires.
    Both real cases are exactly this shape: `note_index` interpolates the embedding width into
    `::vector(N)`, and `job_records` interpolates its column list.
    """
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else " ? "
        for part in node.values
    )


def _sql_literals(path: Path) -> list[str]:
    """Every string in a module that looks like SQL, whitespace-flattened.

    Flattened because these statements are assembled from adjacent string literals across several
    lines, so `INSERT INTO x` and its `ON CONFLICT` clause are rarely on one line — Python has
    already concatenated the plain ones by the time `ast` sees them, and `_joined` does the same
    for the interpolated ones.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(_joined(node))
    return [re.sub(r"\s+", " ", text) for text in texts if _LOOKS_LIKE_SQL.search(text)]


def verbs_the_code_uses() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` for every write `src/` performs.

    An upsert counts as both: Postgres requires UPDATE on the target of
    `ON CONFLICT ... DO UPDATE`, and getting that wrong is an outage the first time two callers
    race on the same key rather than at deploy time.
    """
    known = _tables()
    used: dict[str, set[str]] = {}

    def note(table: str, verb: str) -> None:
        if table in known and table not in _MIGRATOR_ONLY:
            used.setdefault(table, set()).add(verb)

    for path in sorted(_SRC.rglob("*.py")):
        for statement in _sql_literals(path):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                for match in pattern.finditer(statement):
                    note(match.group(1).lower(), verb)
            for match in _UPSERT.finditer(statement):
                note(match.group(1).lower(), "UPDATE")
    for table, verbs in _DYNAMIC.items():
        for verb in verbs:
            note(table, verb)
    return used


def verbs_the_grant_allows() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` as written in the grant file.

    `SELECT` is granted table-wide (`ON ALL TABLES`) and is deliberately not modelled: read is
    uniform, and the boundary worth checking is write.
    """
    # Quotes stripped and whitespace flattened first: a `GRANT` long enough to matter is written as
    # adjacent SQL string literals across several lines, so the statement Postgres assembles is not
    # the text any line-oriented match would see. (Written after a version of this test that read
    # only the single-line grants and reported every other table as ungranted.)
    text = re.sub(r"\s+", " ", _GRANTS.read_text(encoding="utf-8").replace("'", ""))
    allowed: dict[str, set[str]] = {}
    for verbs, tables in re.findall(
        r"GRANT\s+((?:INSERT|UPDATE|DELETE|,|\s)+?)\s+ON\s+((?:\w|,|\s)+?)\s+TO\b", text, re.I
    ):
        granted = {verb.strip().upper() for verb in verbs.split(",") if verb.strip()}
        if not granted <= {"INSERT", "UPDATE", "DELETE"}:
            continue  # `GRANT SELECT ON ALL TABLES` and friends — not part of the write matrix
        for table in (name.strip().lower() for name in tables.split(",")):
            if table:
                allowed.setdefault(table, set()).update(granted)
    return allowed


def test_the_grant_matches_the_writes_the_code_actually_performs() -> None:
    """Neither an outage waiting to happen nor a boundary that has quietly widened."""
    used = verbs_the_code_uses()
    allowed = verbs_the_grant_allows()

    missing = {
        table: sorted(verbs - allowed.get(table, set()))
        for table, verbs in used.items()
        if verbs - allowed.get(table, set())
    }
    assert not missing, (
        f"the code writes what the grant withholds: {missing}. The application would fail with "
        "InsufficientPrivilege on these paths under a split-principal deployment"
    )

    excess = {
        table: sorted(verbs - used.get(table, set()))
        for table, verbs in allowed.items()
        if verbs - used.get(table, set())
    }
    assert not excess, (
        f"the grant allows writes the code never performs: {excess}. A privilege nobody uses is a "
        "privilege that only matters when someone else uses it"
    )


def test_the_audit_trail_is_append_only_by_grant() -> None:
    """The claim `infra/sql/006` has made since it was written, now checked.

    "Append-only by contract" was enforced by nothing — no GRANT, no REVOKE, no trigger, no second
    role in any migration — while the same DSN that ran a chat turn could rewrite the trail
    recording it. A hash chain over the rows used to detect that after the fact; it is gone, so this
    grant is now the whole of the guarantee. Asserted separately from the derivation above because
    it must hold *whatever* the derivation concludes: if a future writer starts issuing
    `UPDATE audit_events`, the right outcome is this test failing, not the grant widening to match.
    """
    allowed = verbs_the_grant_allows()
    assert allowed.get("audit_events") == {"INSERT"}, (
        f"audit_events is granted {sorted(allowed.get('audit_events', set()))}; the trail's whole "
        "integrity claim is that the credential writing a row cannot rewrite it"
    )
    # `audit_anchors` was checked here too while the chain wrote it. The table survives the chain's
    # removal because the schema is forward-only, but nothing writes it, so the correct grant is
    # none at all — asserted rather than merely dropped, because a privilege silently reappearing on
    # a table nobody writes is exactly what the derivation above exists to catch.
    assert "audit_anchors" not in allowed, (
        f"audit_anchors is granted {sorted(allowed.get('audit_anchors', set()))} and no code "
        "writes it; the retired table should carry no privilege"
    )


def test_the_migration_ledger_is_never_granted_to_the_runtime_role() -> None:
    """A role that can write the ledger can mark a migration applied that never ran."""
    assert "schema_migrations" not in verbs_the_grant_allows()


def test_the_grants_are_not_numbered_migrations() -> None:
    """They must re-apply on every deploy, which the tracked, run-once set cannot do.

    A grant is a reconciliation between a schema that keeps growing and a role that may be created
    at any time. As a numbered migration it would apply once: a deployment creating its runtime
    role afterwards would never be granted anything, and every table added by a later migration
    would ship ungranted and break on first use. The runner globs `infra/sql/*.sql`
    non-recursively, so the subdirectory is what keeps them apart.
    """
    from chemclaw.core.grants import grant_files
    from chemclaw.core.migrate import _read_sql_files

    assert _GRANTS.exists()
    assert _GRANTS.name not in _read_sql_files(), (
        "the grant file is inside the tracked migration set, so it would be applied exactly once"
    )
    assert [path.name for path in grant_files()] == [_GRANTS.name]
