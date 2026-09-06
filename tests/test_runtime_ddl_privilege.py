"""The runtime role may create the tables the application creates for itself.

`tests/test_database_privileges.py` derives the *table DML* matrix from the SQL in `src/` and is
deliberately database-free. It is structurally blind to the privilege this file is about, because
the missing one is schema-level DDL rather than a verb against a row — and that blindness had a
consequence: a split-principal deployment provisioned the way `docs/guides/runbook.md` describes
could not take a single turn.

**What was wrong.** Six of the eight tables LangGraph uses are created by the *application*, on
first use, inside the process taking the turn (`AsyncPostgresSaver.setup()`,
`AsyncPostgresStore.setup()`); no migration in `infra/sql` declares them. Under PostgreSQL 15+
`PUBLIC` holds no `CREATE` on schema `public`, `app_privileges.sql` granted only `USAGE`, and
`agent.checkpointer.checkpointer()` has no fallback when `session_store = postgres`. Measured end to
end on a freshly migrated database following the runbook's own steps: both setups raised
`InsufficientPrivilege: permission denied for schema public`.

**Why the grant is the fix and pre-creating the tables is not.** Postgres checks the schema ACL
*before* it checks existence, so `CREATE TABLE IF NOT EXISTS` on a table that already exists still
raises `permission denied for schema public` — exercised below, because it is the whole argument.
And both setups issue exactly that statement on **every process start**, so the privilege is
permanent rather than first-install only.

The second test is live, and it has to be: the question is what Postgres does with an ACL, and only
Postgres can answer it. The first is static, so this file still says something in an environment
with no database.
"""

import re
import uuid

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.grants import grant_files

# The role constant `app_privileges.sql` declares. Substituting it is what lets the live test run
# against a shared database without minting the real cluster-wide `chemclaw_app` role — and the
# substitution is asserted to have matched exactly once, so a test that silently stopped rewriting
# the file, and therefore interrogated a role the file never granted, fails instead of passing.
_ROLE_CONSTANT = "'chemclaw_app'"

_GRANTS_CREATE = re.compile(r"GRANT\s+CREATE\s+ON\s+SCHEMA\s+public\s+TO\s+%I", re.IGNORECASE)


def _grants_sql() -> str:
    """The one grant file, as `make db-grants` applies it."""
    files = grant_files()
    assert len(files) == 1, f"expected one grant file, found {[p.name for p in files]}"
    return files[0].read_text(encoding="utf-8")


def test_the_grant_file_gives_the_runtime_role_create_on_the_schema_it_creates_its_tables_in() -> (
    None
):
    """Static half: the statement exists at all.

    Separate from the live test below because it runs everywhere, including the environments where
    there is no database to connect to — and this is the line whose absence took every turn down.
    """
    assert _GRANTS_CREATE.search(_grants_sql()), (
        "app_privileges.sql grants the runtime role no CREATE on schema public, so "
        "AsyncPostgresSaver.setup() and AsyncPostgresStore.setup() cannot create the tables they "
        "create on first use, and every turn of a split-principal deployment fails with "
        "InsufficientPrivilege"
    )


def test_the_runtime_role_can_actually_create_in_public_after_the_grants_are_applied() -> None:
    """Live half: apply the file to a probe role and make that role run the DDL.

    Two assertions rather than one, and the second is the one that matters:
    `has_schema_privilege` reads the ACL, while `CREATE TABLE` is what the application does. The
    DDL runs inside a transaction that is rolled back, so `public` keeps exactly the tables it had
    — and the `IF NOT EXISTS` arm is executed against a table that already exists, which is the
    measurement the fix rests on.
    """
    try:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")

    role = f"chemclaw_app_probe_{uuid.uuid4().hex[:8]}"
    table = f"probe_ddl_{uuid.uuid4().hex[:8]}"
    rewritten, substitutions = re.subn(_ROLE_CONSTANT, f"'{role}'", _grants_sql())
    assert substitutions == 1, (
        f"expected exactly one {_ROLE_CONSTANT} constant in app_privileges.sql, rewrote "
        f"{substitutions} — this test would otherwise interrogate a role the file never granted"
    )

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cur.fetchone()
            if not row or not row[0]:  # pragma: no cover - env-dependent
                pytest.skip("creating a probe role needs a superuser connection")
            cur.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            with connection.cursor() as cur:
                cur.execute(rewritten)
                cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,))
                granted = cur.fetchone()
            assert granted and granted[0], (
                f"{role} holds no CREATE on schema public after the grant file ran"
            )

            # The DDL itself, as the role. `SET LOCAL ROLE` and the table are both undone by the
            # rollback, so nothing here outlives the transaction.
            connection.autocommit = False
            try:
                with connection.cursor() as cur:
                    cur.execute(f'SET LOCAL ROLE "{role}"')
                    cur.execute(f"CREATE TABLE public.{table} (v integer primary key)")
                    # Existence does not excuse the privilege: Postgres checks the ACL first, which
                    # is why pre-creating these tables in a migration would not have been the fix.
                    cur.execute(f"CREATE TABLE IF NOT EXISTS public.{table} (v integer)")
            finally:
                connection.rollback()
                connection.autocommit = True
        finally:
            with connection.cursor() as cur:
                cur.execute(f'DROP OWNED BY "{role}"')
                cur.execute(f'DROP ROLE "{role}"')
    finally:
        connection.close()
