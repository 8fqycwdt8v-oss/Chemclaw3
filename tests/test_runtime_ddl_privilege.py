"""What Postgres actually decides about the runtime role, measured against a live ACL.

`tests/test_database_privileges.py` compares the text of `src/` with the grant file and cannot
see what Postgres decides. This file applies `infra/sql/grants/app_privileges.sql` to a probe role
and asks the database: the role can `CREATE` in `public` (LangGraph's setups issue `CREATE TABLE
IF NOT EXISTS` on every start, and the schema ACL is checked before existence), the materialised
ACL matches the declared matrix, and the withheld verbs are refused with `42501`.

Everything runs in a rolled-back transaction against a probe role dropped afterwards, with
`search_path` pinned to `public`, because the isolation fixture redirects `postgres_dsn` into a
throwaway schema and unqualified grants resolve through the connection's search_path.
"""

import asyncio
import re
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.grants import apply_grants, grant_files
from tests.test_database_privileges import verbs_the_grant_allows

# The role constant `app_privileges.sql` declares. Substituting it lets the test run without the
# real cluster-wide role; the substitution must match exactly once, or the test would interrogate
# a role the file never granted.
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


def _reconciliation_for(role: str) -> str:
    """The grant file with its role constant rewritten to `role`.

    Asserted to have substituted exactly once, so a test that silently stopped rewriting the file —
    and therefore interrogated a role the file never granted — fails instead of passing.
    """
    rewritten, substitutions = re.subn(_ROLE_CONSTANT, f"'{role}'", _grants_sql())
    assert substitutions == 1, (
        f"expected exactly one {_ROLE_CONSTANT} constant in app_privileges.sql, rewrote "
        f"{substitutions} — this test would otherwise interrogate a role the file never granted"
    )
    return rewritten


def _reconcile_reporting(connection: psycopg.Connection, role: str, drift: list[str]) -> list[str]:
    """Apply `drift`, reconcile, and return what the reconciliation reported — all rolled back.

    Two drifts grant to `PUBLIC` or via membership, not scoped to the probe role, so they must not
    outlive the run on a shared database.
    """
    reported: list[str] = []

    def collect(diagnostic: psycopg.errors.Diagnostic) -> None:
        reported.append(str(diagnostic.message_primary))

    connection.add_notice_handler(collect)
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            for statement in drift:
                cur.execute(statement)
            cur.execute(_reconciliation_for(role))
    finally:
        connection.rollback()
        connection.autocommit = True
        connection.remove_notice_handler(collect)
    return reported


@pytest.fixture
def granted_probe_role() -> Iterator[tuple[psycopg.Connection, str]]:
    """A throwaway role with `app_privileges.sql` applied to it, on a `public`-pinned connection.

    Teardown runs `DROP OWNED BY` before `DROP ROLE`, or the role leaks onto a shared database.
    Skips with no database or without superuser rights to mint a role.
    """
    try:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")

    role = f"chemclaw_app_probe_{uuid.uuid4().hex[:8]}"
    rewritten = _reconciliation_for(role)
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

    `has_table_privilege` is Postgres evaluating its own ACL; the refusals that matter are also
    attempted for a real `42501`.
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
    """The probe role can run the DDL the application runs.

    `has_schema_privilege` reads the ACL; `CREATE TABLE` is what the application does, including the
    `IF NOT EXISTS` arm against an existing table. Rolled back.
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
    """The ACL the grant file materialises is the matrix it declares, checked in the database.

    The file's guarded `EXECUTE format(...)`, blanket `REVOKE` and blanket `GRANT SELECT` can part
    from their text silently. Both directions are checked, restricted to tables `public` has;
    LangGraph's tables appear only after the first turn.
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
    """The verbs the grant withholds are refused by the database, attempted as the role.

    `audit_events` is append-only, `schema_migrations` is the migrator's record, and
    `calculation_results` may not be pruned. Each statement would change nothing even if permitted,
    and the assertion is on `42501` specifically, so an unrelated error does not read as a refusal.
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
    """`GRANT SELECT ON ALL TABLES` reaches every table, including `schema_migrations`.

    Revoking that read must change this assertion together with the grant file's claim.
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


# The four ways the role's effective privileges move without any `GRANT` in `app_privileges.sql`
# changing, each with the drift that creates it and the phrase the reconciliation must report.
# Two of them would allow `UPDATE`/`DELETE` on `audit_events`.
_DRIFT: dict[str, tuple[list[str], str]] = {
    # Membership carries the other role's privileges wholesale, so it reaches every table this file
    # withholds a verb on. `pg_read_all_data` is a predefined role: harmless, always present on
    # PostgreSQL 14+, and enough to put a row in `pg_auth_members`.
    "role membership": (['GRANT pg_read_all_data TO "{role}"'], "is a member of"),
    # A write granted to `PUBLIC` is held by every role in the cluster, and no `REVOKE … FROM
    # <role>` reaches it.
    "a PUBLIC write": (
        ["GRANT UPDATE ON audit_events TO PUBLIC"],
        "PUBLIC holds",
    ),
    # Default privileges apply to tables created *after* they are set, so they re-widen every table
    # a later migration adds, in between two deploys that both looked clean.
    "default privileges": (
        ['ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{role}"'],
        "default privileges",
    ),
    # The reverse direction: `REVOKE ALL ON ALL TABLES` strips a table the app role owns but the
    # file does not name, on the second deploy.
    "an owned table this file does not name": (
        [
            'SET LOCAL ROLE "{role}"',
            "CREATE TABLE public.checkpoint_ninth (v integer primary key)",
            "RESET ROLE",
        ],
        "cannot write",
    ),
}


@pytest.mark.parametrize("channel", sorted(_DRIFT))
def test_the_reconciliation_reports_the_drift_it_cannot_revoke(
    granted_probe_role: tuple[psycopg.Connection, str], channel: str
) -> None:
    """The reconciliation reports the drift it cannot revoke.

    `REVOKE ALL ON ALL TABLES` reaches one of the four ACL sources. Drift is reported as a
    `WARNING` rather than refused, since refusing would block the `pre-upgrade` hook on a hand-grant
    the deploy cannot fix; CI fails on it instead.
    """
    connection, role = granted_probe_role
    drift, phrase = _DRIFT[channel]
    reported = _reconcile_reporting(connection, role, [s.format(role=role) for s in drift])
    assert any(phrase in message for message in reported), (
        f"the reconciliation reported nothing about {channel}. It ran to completion and returned "
        f"the role to what app_privileges.sql declares, while {channel} left it holding privileges "
        f"no GRANT in that file names. Reported: {reported}"
    )


def test_a_clean_reconciliation_reports_no_drift(
    granted_probe_role: tuple[psycopg.Connection, str],
) -> None:
    """A clean reconciliation reports no drift, so the warning means something when it fires."""
    connection, role = granted_probe_role
    assert _reconcile_reporting(connection, role, []) == []


def test_what_the_reconciliation_reports_reaches_the_deploy_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Server `WARNING`s from the reconciliation reach the deploy log.

    psycopg discards notices unless a handler is attached, so the wiring in `chemclaw.core.grants`
    is load-bearing. Driven with the one message raised on a database with no runtime role, which
    mutates nothing.
    """
    try:
        connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")
    with connection:
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (_ROLE_CONSTANT.strip("'"),))
            if cur.fetchone():  # pragma: no cover - env-dependent
                pytest.skip(
                    "this database splits its principal, so the reconciliation does not take its "
                    "no-op branch and would write to a role the suite does not own"
                )

    asyncio.run(apply_grants(settings.postgres_dsn))
    assert "does not exist" in capsys.readouterr().out, (
        "app_privileges.sql raised a message the deploy log never saw; `apply_grants` is not "
        "attaching a notice handler, so the drift audit at the end of that file reports to nobody"
    )
