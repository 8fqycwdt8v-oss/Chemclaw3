"""The grant matrix is derived from the code, not maintained beside it.

`infra/sql/grants/app_privileges.sql` says what the runtime role may write; this derives the same
answer from the SQL literals in `src/` (and upstream's LangGraph SQL) and fails if the two disagree
in **either** direction (D-2026-08-05-append-only-by-grant-not-by-contract):

- a verb the code uses and the grant withholds is an outage on a path nobody exercised;
- a verb the grant allows and the code never uses is the boundary quietly widening, which is how
  an append-only table stops being append-only.

Needs no database: the check is between files in the repository.
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

# Writes whose table reaches the statement through a variable rather than a literal, so no scan of
# the SQL text can see them; named with their authority so each stays true if its source changes.
#
# - The fingerprint stores build `INSERT INTO {table} ... ON CONFLICT DO UPDATE`
#   (`science/fingerprints/store.py`). `corpus_molecules` writes its own literal statement and is
#   seen by the ordinary scan.
# - The retention sweep builds `DELETE FROM {table}` over the closed `_PRUNABLE` map.
#
# LangGraph's checkpointer and store verbs are not listed: `_upstream_verbs()` reads them off the
# installed packages. Our own DELETEs on those tables are literals and found by the scan.
_DYNAMIC: dict[str, set[str]] = {
    "molecule_fingerprints": {"INSERT", "UPDATE"},
    "reaction_fingerprints": {"INSERT", "UPDATE"},
    "corpus_reactions": {"INSERT", "UPDATE"},
    # Every verb here is *added* to what the scans find (`note()` unions into a set), so the sweep's
    # DELETE and upstream's matrix compose rather than one overwriting the other.
    **{table: {"DELETE"} for table in _PRUNABLE},
}

# Written by the migrator alone: the ledger of its own work. A runtime credential that could write
# it could mark a migration applied that never ran, so `migrate.py`'s INSERT is deliberately not
# part of the application's matrix.
_MIGRATOR_ONLY = {"schema_migrations"}

# Modules whose statements belong to an **operator**, not to the running application, and so are
# outside the runtime role's matrix. Named by path because the scan cannot see who runs SQL.
#
# `cli/rekey_campaigns.py` re-keys recorded BO campaigns (computing ids from a stored
# `OptimizationProblem`) under the owning principal; granting the runtime role its DELETE on
# `bo_campaigns` and UPDATE on `bo_suggestions` would let a chat turn rewrite campaign history.
# `cli/rekey_compounds.py --dispose-superseded` issues the fingerprint `DELETE` that
# `infra/sql/094_fingerprint_definition_identity.sql` names as an operator statement, through
# `core.migrate.migration_dsn`, so nothing a turn reaches can prune an index.
_ADMIN_ONLY_MODULES = {"cli/rekey_campaigns.py", "cli/rekey_compounds.py"}

# Modules that build a statement around an **interpolated** table name, mapped to every table they
# can target. `_joined` renders an interpolation as `?` and the verb patterns match `(\w+)`, so
# `DELETE FROM {table}` does not register as a write at all; an undeclared one would fail with
# `permission denied` only on a deployment that runs `make db-grants`.
_INTERPOLATED_TARGETS: dict[str, set[str]] = {
    "science/fingerprints/store.py": {"molecule_fingerprints", "reaction_fingerprints"},
}


def _upstream_tables() -> set[str]:
    """Every table LangGraph's `setup()` creates, derived from the installed distributions.

    These live in the same database but are declared by no file in `infra/sql`. Derived so a table
    upstream adds in a minor bump fails the grant check rather than surfacing as a write outage. The
    store's two version ledgers are named, since upstream spells them inline in `setup()`;
    `tests/test_upstream_surface` pins those names.
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


def _upstream_modules() -> list[Path]:
    """The installed modules whose SQL the two `setup()`s and their writers actually issue.

    The four `_upstream_tables()` reads plus each one's `aio` half (where the DELETEs and ledger
    INSERTs live). `langgraph.checkpoint.postgres.shallow` is excluded: its `DO UPDATE` on
    `checkpoint_blobs` is not what the saver this repository runs issues, and the basis is the code
    that runs.
    """
    from langgraph.checkpoint.postgres import aio as checkpoint_aio
    from langgraph.checkpoint.postgres import base as checkpoint_base
    from langgraph.store.postgres import aio as store_aio
    from langgraph.store.postgres import base as store_base

    return [
        Path(str(module.__file__))
        for module in (checkpoint_base, checkpoint_aio, store_base, store_aio)
    ]


def _verbs_in(paths: list[Path]) -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` for every write the SQL in `paths` performs.

    Factored out of `verbs_the_code_uses()` so it can scan the installed distributions, or a
    synthetic module to prove the derivation reacts to upstream's SQL changing.
    """
    found: dict[str, set[str]] = {}
    for path in paths:
        for statement in _sql_literals(path):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                for match in pattern.finditer(statement):
                    found.setdefault(match.group(1).lower(), set()).add(verb)
            for match in _UPSERT.finditer(statement):
                found.setdefault(match.group(1).lower(), set()).add("UPDATE")
    return found


def _upstream_verbs() -> dict[str, set[str]]:
    """How LangGraph writes each table it creates, read off the distributions that issue the SQL.

    Derived so an upstream bump that turns `ON CONFLICT … DO NOTHING` into `DO UPDATE` forces the
    grant file to move too, rather than failing at the first concurrent write. Narrowed to the
    tables upstream creates, the closed set the grant file's `to_regclass` guards enumerate.
    """
    created = _upstream_tables()
    return {
        table: verbs for table, verbs in _verbs_in(_upstream_modules()).items() if table in created
    }


def _tables() -> set[str]:
    """Every table this database holds: the migrations' and LangGraph's alike.

    `note()` drops tables it does not recognise, so without the upstream half the `_DYNAMIC` entries
    naming them would be discarded silently.
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

    A statement with one interpolation is a `JoinedStr`, and walking for `ast.Constant` alone would
    split `INSERT INTO x` from its `ON CONFLICT ... DO UPDATE` and lose the UPDATE the upsert needs.
    """
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else " ? "
        for part in node.values
    )


def _docstrings(tree: ast.Module) -> set[int]:
    """The `id()` of every docstring constant in `tree` — module, class, function and async.

    Docstrings are constants too, and prose explaining a statement (e.g. why a `CREATE INDEX` is
    rejected) can pass `_LOOKS_LIKE_SQL`, so they are excluded explicitly.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _sql_literals(path: Path) -> list[str]:
    """Every string in a module that looks like SQL, whitespace-flattened, docstrings excluded.

    Flattened because statements are assembled from adjacent literals across lines; Python has
    already concatenated the plain ones, and `_joined` handles the interpolated ones.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = _docstrings(tree)
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose:
                texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(_joined(node))
    return [re.sub(r"\s+", " ", text) for text in texts if _LOOKS_LIKE_SQL.search(text)]


def verbs_the_code_uses() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` for every write `src/` performs.

    An upsert counts as both: Postgres requires UPDATE on the target of `ON CONFLICT ... DO UPDATE`,
    and missing it fails only when two callers race on one key.
    """
    known = _tables()
    used: dict[str, set[str]] = {}

    def note(table: str, verb: str) -> None:
        if table in known and table not in _MIGRATOR_ONLY:
            used.setdefault(table, set()).add(verb)

    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC).as_posix() in _ADMIN_ONLY_MODULES:
            continue
        for statement in _sql_literals(path):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                for match in pattern.finditer(statement):
                    note(match.group(1).lower(), verb)
            for match in _UPSERT.finditer(statement):
                note(match.group(1).lower(), "UPDATE")
    for source in (_DYNAMIC, _upstream_verbs()):
        for table, verbs in source.items():
            for verb in verbs:
                note(table, verb)
    return used


def verbs_the_grant_allows() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` as written in the grant file.

    `SELECT` is granted table-wide (`ON ALL TABLES`) and is deliberately not modelled: read is
    uniform, and the boundary worth checking is write.
    """
    # Quotes stripped and whitespace flattened first: long `GRANT`s are written as adjacent SQL
    # string literals across lines, so no line-oriented match sees the assembled statement.
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


def test_an_upstream_upsert_that_starts_updating_is_seen(tmp_path: Path) -> None:
    """A `DO NOTHING` that becomes a `DO UPDATE` upstream fails here, not at a concurrent write.

    Driven against a synthetic module, because the assertion is that the derivation *reacts*, and
    upstream's real statement must not have to change for that to be provable.
    """
    module = tmp_path / "upstream_probe.py"
    do_nothing = (
        "UPSERT = '''\n"
        "    INSERT INTO checkpoint_blobs (thread_id, channel, version, blob)\n"
        "    VALUES (%s, %s, %s, %s)\n"
        "    ON CONFLICT (thread_id, channel, version) DO NOTHING\n"
        "'''\n"
    )
    module.write_text(do_nothing, encoding="utf-8")
    assert _verbs_in([module]) == {"checkpoint_blobs": {"INSERT"}}

    module.write_text(do_nothing.replace("DO NOTHING", "DO UPDATE SET blob = EXCLUDED.blob"))
    assert _verbs_in([module]) == {"checkpoint_blobs": {"INSERT", "UPDATE"}}


def test_the_upstream_verbs_are_read_off_the_distributions_that_issue_them() -> None:
    """Every table upstream creates is a table upstream's own SQL says how it writes.

    Both halves are derived from the same place: a table with no verb is a grant nobody can check,
    and a verb on a table upstream no longer creates is a privilege on nothing.
    """
    derived = _upstream_verbs()
    assert set(derived) == _upstream_tables(), sorted(set(derived) ^ _upstream_tables())
    assert all("INSERT" in verbs for verbs in derived.values()), derived


def test_a_write_against_an_interpolated_table_is_declared_for_every_table_it_can_hit() -> None:
    """The scan cannot read a table out of `DELETE FROM {table}`, so the verb must be declared.

    Reads the verbs back out of each interpolating module and requires `_DYNAMIC` to allow each for
    *every* table the module can target. Fewer verbs than granted is fine; the failure is a verb the
    code performs and the grant withholds.
    """
    for module, tables in _INTERPOLATED_TARGETS.items():
        verbs: set[str] = set()
        for statement in _sql_literals(_SRC / module):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                if pattern.search(statement.replace(" ? ", " x ")):
                    verbs.add(verb)
            if _UPSERT.search(statement.replace(" ? ", " x ")):
                verbs.add("UPDATE")
        for table in sorted(tables):
            missing = verbs - _DYNAMIC.get(table, set())
            assert not missing, (
                f"{module} performs {sorted(missing)} against an interpolated table name, and "
                f"`_DYNAMIC[{table!r}]` does not allow it. Either the grant file must grant it on "
                f"{table} and `_DYNAMIC` record it, or the statement must go. Note the scan cannot "
                "see which table an interpolated statement hit, which is why this is checked "
                "against every table the module can target."
            )


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
    """The audit trail is append-only by grant.

    The grant is the whole guarantee, so this holds whatever the derivation concludes: if a writer
    starts issuing `UPDATE audit_events`, this test must fail rather than the grant widen to match.
    """
    allowed = verbs_the_grant_allows()
    assert allowed.get("audit_events") == {"INSERT"}, (
        f"audit_events is granted {sorted(allowed.get('audit_events', set()))}; the trail's whole "
        "integrity claim is that the credential writing a row cannot rewrite it"
    )
    # `audit_anchors` survives in the forward-only schema but nothing writes it, so the correct
    # grant is none; asserted so a privilege reappearing on it is caught.
    assert "audit_anchors" not in allowed, (
        f"audit_anchors is granted {sorted(allowed.get('audit_anchors', set()))} and no code "
        "writes it; the retired table should carry no privilege"
    )


def test_an_operator_module_exists_and_its_fingerprint_disposal_is_never_the_runtime_roles() -> (
    None
):
    """The exclusion list names real files, and excluding them still grants no `DELETE`.

    A stale path excludes nothing and reads as a reason. Whatever the operator's command deletes,
    the runtime role may only insert and update a fingerprint index.
    """
    missing = sorted(path for path in _ADMIN_ONLY_MODULES if not (_SRC / path).is_file())
    assert not missing, f"_ADMIN_ONLY_MODULES names files that do not exist: {missing}"
    allowed = verbs_the_grant_allows()
    for table in ("molecule_fingerprints", "reaction_fingerprints"):
        assert "DELETE" not in allowed.get(table, set()), (
            f"the runtime role may DELETE from {table}; disposing of a superseded generation is an "
            "operator statement under the schema owner (094), not a privilege a turn holds"
        )


def test_the_migration_ledger_is_never_granted_a_write_verb() -> None:
    """A role that can write the ledger can mark a migration applied that never ran.

    This checks write verbs only: the blanket `GRANT SELECT` does reach the ledger, intentionally
    (`app_privileges.sql` says so). `tests/test_runtime_ddl_privilege.py` checks the live ACL.
    """
    assert "schema_migrations" not in verbs_the_grant_allows()


def test_the_grants_are_not_numbered_migrations() -> None:
    """The grants must re-apply on every deploy, which the tracked, run-once migrations cannot do.

    As a numbered migration they would apply once, so a role created later, or a table added by a
    later migration, would ship ungranted. The runner globs `infra/sql/*.sql` non-recursively, so
    the subdirectory keeps them apart.
    """
    from chemclaw.core.grants import grant_files
    from chemclaw.core.migrate import _read_sql_files

    assert _GRANTS.exists()
    assert _GRANTS.name not in _read_sql_files(), (
        "the grant file is inside the tracked migration set, so it would be applied exactly once"
    )
    assert [path.name for path in grant_files()] == [_GRANTS.name]


# The chart document that applies the reconciliation. Read as text rather than rendered, because
# what is asserted below is which points in a release's life the Job is attached to, and that is an
# annotation Helm reads off the manifest rather than anything a rendered value can show.
_MIGRATE_JOB = _ROOT / "deploy" / "helm" / "chemclaw" / "templates" / "migrate-job.yaml"


def test_a_rolled_back_release_re_applies_its_own_grant_file() -> None:
    """The reconciliation is a full restatement, so it **narrows**, and a rollback must undo that.

    `app_privileges.sql` revokes first, so a verb removed from the file is revoked from the role.
    `helm rollback` runs neither upgrade hook, so without `pre-rollback` the older image would run
    against the newer, narrower ACL until the next deploy. `pre-` so the restored pods find their
    ACL already in place.
    """
    migrate = _MIGRATE_JOB.read_text(encoding="utf-8").split("\n---\n")[0]
    # Selects the migrate Job by the component it dispatches: the grants command lives in
    # `deploy/entrypoint.sh` (so the Job goes through the image ENTRYPOINT and its egress layer),
    # and the migrations-then-grants order is asserted below against that script.
    assert re.search(r'value:\s*"?migrate"?', migrate), (
        "wrong document: this one is not the migrate Job"
    )
    entrypoint = (_ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    case = entrypoint.split("migrate)", 1)[-1].split(";;", 1)[0]
    assert "python -m chemclaw.core.migrate" in case and "python -m chemclaw.core.grants" in case, (
        "the migrate component no longer runs both halves, so a release applies schema without "
        "reconciling the runtime role's grants"
    )
    assert case.index("chemclaw.core.migrate") < case.index("chemclaw.core.grants"), (
        "grants run before the migrations that create the tables they name, and a grant applied "
        "before its table exists fails"
    )
    assert re.search(r'"helm\.sh/hook":[^\n]*\bpre-rollback\b', migrate), (
        "the Job that reconciles the grants does not run on rollback, so `helm rollback` restores "
        "the older image against the newer release's ACL — and the grant set contracts, so that "
        "ACL can be strictly narrower than the restored image needs"
    )


def test_the_chart_does_not_claim_the_grants_only_widen() -> None:
    """The chart must not claim the grants only widen.

    The grant file narrows when a verb is removed, so that sentence would justify a hook ordering
    that lands a contraction on the still-serving release.
    """
    for path in (_MIGRATE_JOB, _GRANTS):
        text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        assert "grants only widen" not in text, (
            f"{path.name} says the grants only widen. They do not: the file is a full restatement, "
            "so a verb removed from it is revoked from the role on the next deploy"
        )


# Modules whose SQL literals are the *migrator's*: `core/migrate.py` (`make db-migrate`) and
# `core/grants.py` (applies `app_privileges.sql`). DDL anywhere else is a runtime process issuing
# it.
_MIGRATOR_MODULES = {"core/migrate.py", "core/grants.py"}

# DDL a *runtime* process must never issue. `CREATE INDEX` is deliberately absent from the pattern's
# own vocabulary only in the sense that it is covered: the point is the schema-level right, and a
# first-party `CREATE INDEX` on a table it does not own needs the same conversation.
_DDL = re.compile(
    r"\b(CREATE|DROP)\s+(TABLE|INDEX|SCHEMA|EXTENSION|FUNCTION|VIEW|SEQUENCE)\b|\bALTER\s+TABLE\b",
    re.I,
)


def test_the_only_ddl_a_runtime_process_issues_is_upstreams_setup() -> None:
    """The only DDL a runtime process issues is upstream's `setup()`.

    `GRANT CREATE ON SCHEMA public` is kept because the only runtime DDL is LangGraph's
    `AsyncPostgresSaver.setup()` / `AsyncPostgresStore.setup()`, under an advisory lock in
    `agent/checkpointer.py::_setup_once`
    (`D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns`). A first-party module
    issuing DDL changes that premise, so it fails here until the decision is revisited — a declared
    exception, not a ban.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative in _MIGRATOR_MODULES:
            continue
        for statement in _sql_literals(path):
            if _DDL.search(statement):
                offenders.append(f"{relative}: {statement[:90]}")
    assert offenders == [], (
        "a runtime module issues DDL, so the runtime role's `CREATE ON SCHEMA public` is no longer "
        "bounded by what upstream's checkpointer needs — revisit "
        "D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns before adding it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_store_tables_are_created_before_the_grants_that_name_them() -> None:
    """The store tables are created before the grants that name them.

    `store` and `store_migrations` are created by `AsyncPostgresStore.setup()`, and the grant file
    grants on them only `IF to_regclass(...) IS NOT NULL`. The migrate Job runs before any app pod,
    so it must create them first or a fresh install's runtime role gets no write on `store` until
    the next release. Reads the script, since a step that exists but is never called is the failure.
    """
    entrypoint = (_ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    case = entrypoint.split("migrate)", 1)[-1].split(";;", 1)[0]

    assert "python -m chemclaw.agent.store_setup" in case, (
        "the migrate component does not create the store's tables, so the grants that name them "
        "find nothing and the runtime role cannot write to `store` until the next release"
    )
    assert (
        case.index("chemclaw.core.migrate")
        < case.index("chemclaw.agent.store_setup")
        < case.index("chemclaw.core.grants")
    ), (
        "the three steps of the migrate role are out of order: migrations, then the store's own "
        "tables, then the grants that name them — any other order grants on something absent"
    )


def test_the_store_setup_step_runs_as_the_migrator() -> None:
    """The store setup step runs as the migrator.

    `agent/scratchpad.memory_store()` uses the runtime credential, which on a fresh install has no
    `CREATE` yet (granting it is the next step), so reusing it would deadlock the install. Asserted
    on `migration_dsn()`, the answer `core/migrate.py` and `core/grants.py` already share.
    """
    import inspect

    from chemclaw.agent import store_setup

    tree = ast.parse(inspect.getsource(store_setup))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }

    assert "migration_dsn" in called, (
        "the store setup step does not resolve the migrator's credential, so it runs as whatever "
        f"the runtime role is — which on a fresh install cannot CREATE in the schema. Calls: "
        f"{sorted(called)}"
    )
    # An AST walk rather than a substring, because this module *names* `memory_store` in its
    # docstring to say why it is not that function — and a grep would read the explanation as the
    # defect it exists to explain.
    assert "memory_store" not in called, (
        "the store setup step reuses the process-wide store, which builds over the checkpointer's "
        "pool: that is the runtime credential, and it has no CREATE until the grants run"
    )
