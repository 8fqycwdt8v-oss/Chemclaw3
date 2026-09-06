"""What Postgres actually decides about the runtime role, measured against a live ACL.

`tests/test_database_privileges.py` derives the *table DML* matrix from the SQL in `src/` and is
deliberately database-free — it compares the text of `src/` with the text of
`infra/sql/grants/app_privileges.sql`. That is the right shape for a declaration check and it is
structurally blind to everything Postgres decides: a table dropped and recreated by a later
migration, an `EXECUTE format` typo inside a `to_regclass` guard, or a `REVOKE` reaching further
than intended all pass a comparison of two regexes. This file is the other half — the one that
connects, applies the grant file to a probe role, and asks the database.

**It is named for the regression that created it**, which was schema-level DDL rather than a verb
against a row: six of the eight tables LangGraph uses are created by the *application*, on first
use, inside the process taking the turn (`AsyncPostgresSaver.setup()`, `AsyncPostgresStore.setup()`)
and no migration in `infra/sql` declares them. Under PostgreSQL 15+ `PUBLIC` holds no `CREATE` on
schema `public`, `app_privileges.sql` granted only `USAGE`, and `agent.checkpointer.checkpointer()`
has no fallback when `session_store = postgres`. Measured end to end on a freshly migrated database
following `docs/guides/runbook.md`'s own steps: both setups raised `InsufficientPrivilege:
permission denied for schema public`.

**Why the grant is the fix and pre-creating the tables is not.** Postgres checks the schema ACL
*before* it checks existence, so `CREATE TABLE IF NOT EXISTS` on a table that already exists still
raises `permission denied for schema public` — exercised below, because it is the whole argument.
And both setups issue exactly that statement on **every process start**, so the privilege is
permanent rather than first-install only.

**And for a long time DDL was all it measured**, while the file's own reasoning ("the question is
what Postgres does with an ACL, and only Postgres can answer it") applies just as much to the
append-only guarantee one directory over. No table verb was ever attempted as the role anywhere in
this repository: the audit trail's INSERT-only posture, the ledger's, and the retention refusals
that `app_privileges.sql` turns from intentions into enforcement were claimed by a regex over the
grant file and by nothing else. They hold — measured, `42501` on `UPDATE`/`DELETE audit_events` and
on writes to `schema_migrations` — which makes what was missing a *ratchet* rather than a defect,
and a ratchet is only worth having before the posture drifts.

Everything here runs in a transaction that is rolled back, against a probe role dropped on the way
out, with the connection's `search_path` pinned to `public` — the schema the grant file names in its
`ON ALL TABLES` statements, and the one a deployment's tables live in. Pinned rather than inherited
because the session-wide isolation fixture redirects `postgres_dsn` into a throwaway schema, and a
bare `GRANT INSERT ON audit_events` resolves through the *connection's* search_path: unpinned, this
file would grant on one schema's tables and the blanket statements on another's.
"""

import re
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.grants import grant_files
from tests.test_database_privileges import verbs_the_grant_allows

# The role constant `app_privileges.sql` declares. Substituting it is what lets the live test run
# against a shared database without minting the real cluster-wide `chemclaw_app` role — and the
# substitution is asserted to have matched exactly once, so a test that silently stopped rewriting
# the file, and therefore interrogated a role the file never granted, fails instead of passing.
_ROLE_CONSTANT = "'chemclaw_app'"

_GRANTS_CREATE = re.compile(r"GRANT\s+CREATE\s+ON\s+SCHEMA\s+public\s+TO\s+%I", re.IGNORECASE)

# The table whose absence means `public` was never migrated. Named rather than inferred, because
# every assertion below is about the ACL on a *set of tables*, and an empty set passes all of them.
_SENTINEL_TABLE = "audit_events"

# The verbs modelled. `SELECT` is granted `ON ALL TABLES` and is deliberately outside the write
# matrix `verbs_the_grant_allows()` derives — it is asserted on its own below, as uniformity.
_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE")


def _grants_sql() -> str:
    """The one grant file, as `make db-grants` applies it."""
    files = grant_files()
    assert len(files) == 1, f"expected one grant file, found {[p.name for p in files]}"
    return files[0].read_text(encoding="utf-8")


@pytest.fixture
def granted_probe_role() -> Iterator[tuple[psycopg.Connection, str]]:
    """A throwaway role with `app_privileges.sql` applied to it, on a `public`-pinned connection.

    One fixture rather than the minting block repeated per test: the setup is four steps that must
    all be undone (create role, rewrite the constant, apply the file, `DROP OWNED BY` before
    `DROP ROLE`), and getting the last pair wrong leaks a role onto a shared database that the next
    run cannot drop either.

    Skips rather than fails on the two environments it cannot run in, both named in the reason: no
    database, and a non-superuser connection that cannot mint a role.
    """
    try:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")

    role = f"chemclaw_app_probe_{uuid.uuid4().hex[:8]}"
    rewritten, substitutions = re.subn(_ROLE_CONSTANT, f"'{role}'", _grants_sql())
    assert substitutions == 1, (
        f"expected exactly one {_ROLE_CONSTANT} constant in app_privileges.sql, rewrote "
        f"{substitutions} — this test would otherwise interrogate a role the file never granted"
    )
    try:
        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            if not row or not row[0]:  # pragma: no cover - env-dependent
                pytest.skip("creating a probe role needs a superuser connection")
            cur.execute("SELECT to_regclass(%s)", (f"public.{_SENTINEL_TABLE}",))
            migrated = cur.fetchone()
            if not migrated or migrated[0] is None:  # pragma: no cover - env-dependent
                pytest.skip(
                    f"public holds no {_SENTINEL_TABLE}, so there is no ACL to interrogate — "
                    "run `make db-migrate` (CI does, before this suite)"
                )
            cur.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            with connection.cursor() as cur:
                cur.execute(rewritten)
            yield connection, role
        finally:
            with connection.cursor() as cur:
                cur.execute(f'DROP OWNED BY "{role}"')
                cur.execute(f'DROP ROLE "{role}"')
    finally:
        connection.close()


def _live_write_matrix(connection: psycopg.Connection, role: str) -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` as the database holds it, for every table in `public`.

    `has_table_privilege` is Postgres evaluating its own ACL — the same evaluation the executor
    makes — so this reads the whole schema in one pass rather than attempting 144 statements. The
    statements that *are* attempted are the handful whose refusal is the point (below): a real
    `42501` is the only evidence that the ACL is enforced and not merely reported.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        tables = [str(row[0]) for row in cur.fetchall()]
        matrix: dict[str, set[str]] = {}
        for table in tables:
            held = set()
            for verb in _WRITE_VERBS:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", verb)
                )
                answer = cur.fetchone()
                if answer and answer[0]:
                    held.add(verb)
            if held:
                matrix[table] = held
    return matrix


def test_the_grant_file_gives_the_runtime_role_create_on_the_schema_it_creates_its_tables_in() -> (
    None
):
    """Static half: the statement exists at all.

    Separate from the live tests below because it runs everywhere, including the environments where
    there is no database to connect to — and this is the line whose absence took every turn down.
    """
    assert _GRANTS_CREATE.search(_grants_sql()), (
        "app_privileges.sql grants the runtime role no CREATE on schema public, so "
        "AsyncPostgresSaver.setup() and AsyncPostgresStore.setup() cannot create the tables they "
        "create on first use, and every turn of a split-principal deployment fails with "
        "InsufficientPrivilege"
    )


def test_the_runtime_role_can_actually_create_in_public_after_the_grants_are_applied(
    granted_probe_role: tuple[psycopg.Connection, str],
) -> None:
    """Live half: make the probe role run the DDL the application runs.

    Two assertions rather than one, and the second is the one that matters:
    `has_schema_privilege` reads the ACL, while `CREATE TABLE` is what the application does. The
    DDL runs inside a transaction that is rolled back, so `public` keeps exactly the tables it had
    — and the `IF NOT EXISTS` arm is executed against a table that already exists, which is the
    measurement the fix rests on.
    """
    connection, role = granted_probe_role
    table = f"probe_ddl_{uuid.uuid4().hex[:8]}"
    with connection.cursor() as cur:
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,))
        granted = cur.fetchone()
    assert granted and granted[0], (
        f"{role} holds no CREATE on schema public after the grant file ran"
    )

    # The DDL itself, as the role. `SET LOCAL ROLE` and the table are both undone by the rollback,
    # so nothing here outlives the transaction.
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(f'SET LOCAL ROLE "{role}"')
            cur.execute(f"CREATE TABLE public.{table} (v integer primary key)")
            # Existence does not excuse the privilege: Postgres checks the ACL first, which is why
            # pre-creating these tables in a migration would not have been the fix.
            cur.execute(f"CREATE TABLE IF NOT EXISTS public.{table} (v integer)")
    finally:
        connection.rollback()
        connection.autocommit = True


def test_the_acl_the_grant_file_materialises_is_the_matrix_it_declares(
    granted_probe_role: tuple[psycopg.Connection, str],
) -> None:
    """The derived matrix, checked against the database instead of against a second text.

    `tests/test_database_privileges.py` proves the grant file's *statements* match the writes `src/`
    performs. Nothing proved the statements produce the ACL they read like — and the file is a
    ~280-line `DO $$` block of `EXECUTE format(...)` with per-table `to_regclass` guards, an
    indiscriminate `REVOKE ALL ON ALL TABLES` at the top, and a blanket `GRANT SELECT` after it.
    Every one of those is a place where the text and the outcome can part company silently.

    Both directions, for the reasons the static test gives: a verb the role lacks is an outage on a
    path nobody exercised before the deploy, and a verb it holds and no code uses is the boundary
    widening back out. Restricted to tables `public` actually has, because the LangGraph store's
    tables are created by the application on first use and a database that has never taken a turn
    does not have them — their absence is reported by `tests/conftest.py`, not asserted here.
    """
    connection, role = granted_probe_role
    live = _live_write_matrix(connection, role)
    with connection.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        present = {str(row[0]) for row in cur.fetchall()}
    declared = {
        table: verbs for table, verbs in verbs_the_grant_allows().items() if table in present
    }

    withheld = {
        table: sorted(verbs - live.get(table, set()))
        for table, verbs in declared.items()
        if verbs - live.get(table, set())
    }
    assert not withheld, (
        f"the grant file declares {withheld} and the database does not hold it after the file ran. "
        "The application would meet InsufficientPrivilege on these paths while every text-level "
        "check stayed green"
    )
    surplus = {
        table: sorted(verbs - declared.get(table, set()))
        for table, verbs in live.items()
        if verbs - declared.get(table, set())
    }
    assert not surplus, (
        f"the role holds {surplus} and no GRANT in app_privileges.sql names it — a privilege that "
        "arrived from somewhere other than the file that is supposed to be the whole matrix"
    )


def test_the_verbs_the_grant_withholds_are_refused_by_the_database(
    granted_probe_role: tuple[psycopg.Connection, str],
) -> None:
    """The refusals the whole file exists for, attempted as the role rather than asserted about.

    `audit_events` is the append-only trail, and since the hash chain was removed
    (D-2026-08-14) this grant is the entire guarantee: the credential that writes a row cannot
    rewrite or remove it. `schema_migrations` is the migrator's record of its own work — a runtime
    credential able to write it could mark a migration applied that never ran.
    `calculation_results` is one of the tables `durable/retention.py` refuses to prune, and
    withholding DELETE is what makes that refusal enforced rather than intended.

    Each statement is written so that a role holding the privilege would still change nothing
    (`WHERE false`, and the one INSERT is rolled back), because the assertion is about the error
    code and not about the row: `42501` (`insufficient_privilege`) rather than any failure, since a
    typo failing on `42703` would otherwise read as a refusal.
    """
    connection, role = granted_probe_role
    refused = {
        "UPDATE audit_events": "UPDATE audit_events SET actor = actor WHERE false",
        "DELETE audit_events": "DELETE FROM audit_events WHERE false",
        "INSERT schema_migrations": (
            "INSERT INTO schema_migrations (filename, checksum) VALUES ('probe', 'probe')"
        ),
        "UPDATE schema_migrations": (
            "UPDATE schema_migrations SET checksum = checksum WHERE false"
        ),
        "DELETE calculation_results": "DELETE FROM calculation_results WHERE false",
    }
    connection.autocommit = False
    try:
        for label, statement in refused.items():
            try:
                with connection.cursor() as cur:
                    cur.execute(f'SET LOCAL ROLE "{role}"')
                    cur.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            except psycopg.Error as exc:  # pragma: no cover - a probe statement that went stale
                raise AssertionError(
                    f"{label} failed with {exc.sqlstate} rather than being refused: {exc}. The "
                    "probe statement no longer matches the schema, so it proves nothing"
                ) from exc
            finally:
                connection.rollback()
            raise AssertionError(
                f"{label} was ALLOWED as the runtime role. app_privileges.sql withholds it "
                "deliberately, and this is the grant that does the withholding"
            )
    finally:
        connection.rollback()
        connection.autocommit = True


def test_read_is_uniform_and_reaches_the_migration_ledger(
    granted_probe_role: tuple[psycopg.Connection, str],
) -> None:
    """`GRANT SELECT ON ALL TABLES` means every table, and the ledger is not an exception.

    Asserted because the grant file said otherwise in prose for as long as the sentence existed:
    "`schema_migrations` is deliberately absent from every GRANT above" was true of every *write*
    grant and false of the blanket read, which names no table and therefore cannot be seen by
    `tests/test_database_privileges.py`'s regex over the named `GRANT` statements. Measured, the
    role holds `SELECT` on it. The load-bearing half of that sentence — no write verb — is proven
    by the test above; this one pins the half that was wrong, so a deployment that decides to
    revoke the read has to change the claim and this assertion together rather than leaving a third
    version of the sentence standing.
    """
    connection, role = granted_probe_role
    with connection.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        tables = [str(row[0]) for row in cur.fetchall()]
        unreadable = []
        for table in tables:
            cur.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", (role, f"public.{table}"))
            answer = cur.fetchone()
            if not answer or not answer[0]:
                unreadable.append(table)
    assert not unreadable, (
        f"the runtime role cannot read {unreadable}; `GRANT SELECT ON ALL TABLES IN SCHEMA public` "
        "is what makes read uniform, and a table it misses is an outage on first use"
    )
    assert "schema_migrations" in tables, "public holds no migration ledger to check"
