"""Two tenants' data behind the real front door, for the cross-tenant leak test.

The application connects as a role that is neither a superuser nor `BYPASSRLS` (`tenancy_db`), with
the policies on and two tenants registered. The same person (one Entra object id) holds a token in
each tenant, because that is the sharpest question to ask: nothing but the tenant separates what
they may read in one from what they may read in the other, so a route that leaks does so even to
somebody who owns the row.

`World.call` is the only way a test reaches the app, and it keeps every response body so the test
can search them all for the other tenant's markers.
"""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import chemclaw.agent.session_members as members_module
import chemclaw.api.auth as auth
from chemclaw.agent.plan_approval_store import plan_approval_store
from chemclaw.api.app import create_app
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenancy import bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database

ACME = "acme"
GLOBEX = "globex"
#: One person, in both tenants.
PERSON = "person-1"
#: A second person, for membership.
OTHER = "person-2"
#: The role the deployment names privileged in these tests.
REVIEWER = "reviewer"
#: Every row seeded for ACME carries this, so a response can be searched for it.
MARKER = "ACME-SECRET"

_AUDIENCE = "api://chemclaw"
_ISSUER = "https://issuer.test/v2.0"


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connector is attached: the leak test is about rows, not tools."""
    return []


@dataclass
class World:
    """The app, its database and a way to call it as a tenant."""

    database: TenancyDatabase
    app: FastAPI
    key: Any
    #: Every response this world has produced: tenant, request, status and body.
    bodies: list[tuple[str, str, int, str]] = field(default_factory=list)

    def token(self, tenant: str, oid: str = PERSON, roles: tuple[str, ...] = ()) -> str:
        """A signed token for `oid` acting in `tenant`."""
        pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        claims = {
            "aud": _AUDIENCE,
            "iss": _ISSUER,
            "exp": int(time.time()) + 3600,
            "oid": oid,
            "roles": [f"tenant:{tenant}", *roles],
        }
        return str(jwt.encode(claims, pem, algorithm="RS256"))

    def call(
        self,
        tenant: str,
        method: str,
        path: str,
        *,
        oid: str = PERSON,
        roles: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> Any:
        """One request as `oid` in `tenant`; its body is kept for the search for markers."""
        headers = {"Authorization": f"Bearer {self.token(tenant, oid, roles)}"}
        response = TestClient(self.app).request(method, path, headers=headers, **kwargs)
        self.bodies.append((tenant, f"{method} {path}", response.status_code, response.text))
        return response

    @asynccontextmanager
    async def acting(self, tenant: str) -> AsyncIterator[None]:
        """Run seeding code as `tenant`: every store it uses borrows connections for it."""
        with bound_tenant(tenant):
            yield

    async def owner_rows(self, table: str, tenant: str) -> list[str]:
        """Every row of `table` that belongs to `tenant`, as text, read as the superuser."""
        async with await connect(self.database.owner_dsn) as conn:
            cursor = await conn.execute(
                f"SELECT t::text FROM {table} t WHERE tenant_id = %s ORDER BY 1", (tenant,)
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    async def snapshot(self, tenant: str) -> dict[str, list[str]]:
        """Every scoped table's rows for `tenant`, which other tenants' requests must not change."""
        async with await connect(self.database.owner_dsn) as conn:
            cursor = await conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
                "WHERE c.relkind = 'r' AND c.relnamespace = current_schema()::regnamespace "
                "AND a.attname = 'tenant_id' ORDER BY 1"
            )
            tables = [str(row[0]) for row in await cursor.fetchall()]
        return {table: await self.owner_rows(table, tenant) for table in tables}


@asynccontextmanager
async def two_tenant_world(
    monkeypatch: pytest.MonkeyPatch,
    customise: Callable[[FastAPI], None] | None = None,
    *,
    as_superuser: bool = False,
) -> AsyncIterator[World]:
    """The production app on a private schema, as a role the policies bind, with tenancy on.

    `as_superuser` connects the application as the suite's superuser instead, which no policy
    binds: the control that shows the leak test would notice a leak.
    """
    async with tenancy_database() as database:
        await enable_tenancy(database, ACME, GLOBEX)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        monkeypatch.setattr(
            settings, "postgres_dsn", database.owner_dsn if as_superuser else database.app_dsn
        )
        monkeypatch.setattr(settings, "session_store", "postgres")
        monkeypatch.setattr(settings, "tenancy_enabled", True)
        monkeypatch.setattr(settings, "tenancy_process_tenant", "")
        monkeypatch.setattr(settings, "entra_required", True)
        monkeypatch.setattr(settings, "entra_tenant_id", "tenant-for-the-leak-test")
        monkeypatch.setattr(settings, "entra_audience", _AUDIENCE)
        monkeypatch.setattr(settings, "entra_issuer", _ISSUER)
        monkeypatch.setattr(settings, "entra_privileged_roles", REVIEWER)
        monkeypatch.setattr(auth, "_signing_key", lambda _token: key.public_key())
        forget_registered_tenants()
        members_module.session_member_store.cache_clear()
        plan_approval_store.cache_clear()
        app = create_app(connector_factory=_no_connectors)
        if customise is not None:
            customise(app)
        try:
            yield World(database=database, app=app, key=key)
        finally:
            members_module.session_member_store.cache_clear()
            plan_approval_store.cache_clear()
            forget_registered_tenants()
