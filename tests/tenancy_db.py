"""A private, migrated schema and a database role that row-level security applies to.

The suite's own connection is a superuser, which no policy constrains, so a test of isolation needs
a second principal: one that is neither a superuser nor `BYPASSRLS`. `tenancy_database` migrates a
schema of its own (the shared test schema is used by every other test in the session and must keep
its policies disabled), creates that role, grants it the verbs the application holds on every
table, and yields both DSNs. The role and the schema are dropped on exit.

Tests that need the policies apply them with `enable_tenancy`; the rest of the module is plumbing.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migrate
from tests.pg import create_test_schema, drop_test_schema, schema_dsn


@dataclass(frozen=True)
class TenancyDatabase:
    """The two ways into one private schema."""

    schema: str
    #: The suite's superuser, owner of every table: policies do not apply to it.
    owner_dsn: str
    #: A login role that is neither a superuser nor `BYPASSRLS`, holding DML on every table.
    app_dsn: str
    role: str


def _unredirected(dsn: str) -> str:
    """`dsn` without the isolation schema's `options`, for creating schemas and roles."""
    return dsn.split("?options=", 1)[0]


def _with_credentials(dsn: str, user: str, password: str) -> str:
    """`dsn` (a URL) naming another login."""
    parts = urlsplit(_unredirected(dsn))
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    return f"postgresql://{quote(user)}:{quote(password)}@{host}{port}{parts.path}"


async def _owner_connection(base: str) -> psycopg.AsyncConnection:
    """A dedicated connection as the suite's superuser, outside any isolation schema."""
    return await connect(base)


@asynccontextmanager
async def tenancy_database() -> AsyncIterator[TenancyDatabase]:
    """Migrate a private schema and create a role that row-level security applies to.

    Skips when Postgres is unreachable, as every database test does. The policies are created by the
    migration and left disabled; `enable_tenancy` switches them on.
    """
    base = _unredirected(str(settings.postgres_dsn))
    suffix = secrets.token_hex(5)
    schema = f"chemclaw_tenancy_{suffix}"
    role = f"chemclaw_rls_{suffix}"
    password = secrets.token_hex(8)
    try:
        await create_test_schema(base, schema)
    except (psycopg.Error, ConnectionError) as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")
    owner_dsn = schema_dsn(base, schema)
    try:
        await migrate(owner_dsn)
        async with await connect(base) as conn:
            # The password is hex, generated above: DDL takes no bind parameter for it.
            await conn.execute(
                f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE"
            )
            await conn.execute(f'GRANT USAGE ON SCHEMA "{schema}", public TO "{role}"')
            await conn.execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
                f'TO "{role}"'
            )
            await conn.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" TO "{role}"')
            await conn.commit()
        yield TenancyDatabase(
            schema=schema,
            owner_dsn=owner_dsn,
            app_dsn=schema_dsn(_with_credentials(base, role, password), schema),
            role=role,
        )
    finally:
        await drop_test_schema(base, schema)
        async with await connect(base) as conn:
            exists = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if await exists.fetchone() is not None:
                await conn.execute(f'DROP OWNED BY "{role}"')
                await conn.execute(f'DROP ROLE "{role}"')
            await conn.commit()


async def enable_tenancy(database: TenancyDatabase, *tenants: str) -> None:
    """Register `tenants` and switch the policies on, as the migrator does when asked.

    Registered before the switch, because switching off is refused with more than one.
    """
    async with await connect(database.owner_dsn) as conn:
        for tenant in tenants:
            await conn.execute(
                "INSERT INTO tenants (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (tenant,)
            )
        await conn.execute("SELECT chemclaw_apply_tenancy(true)")
        await conn.commit()
