"""The authenticated principal names the tenant, and the request carries it to the database.

A token's one `tenant:<id>` role is the tenant; a token with none, with two, or with an id nobody
registered is refused with a 403 before any route runs. With tenancy off nothing is read from the
roles and the tenant is `default`, so a deployment that never turned it on is unchanged.
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import chemclaw.api.auth as auth
from chemclaw.api.app import create_app
from chemclaw.api.auth import AuthError, Principal, require_principal, validate_token
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import DEFAULT_TENANT, current_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from tests.pg import migrated_db_or_skip

_AUDIENCE = "api://chemclaw"
_ISSUER = "https://issuer.test/v2.0"


@pytest.fixture
def rsa_key() -> Any:
    """A fresh signing key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key: Any, *, roles: list[str]) -> str:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    claims = {
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "exp": int(time.time()) + 3600,
        "oid": "u-1",
        "roles": roles,
    }
    return jwt.encode(claims, pem, algorithm="RS256")


@pytest.fixture(autouse=True)
def _entra(monkeypatch: pytest.MonkeyPatch, rsa_key: Any) -> None:
    monkeypatch.setattr(settings, "entra_audience", _AUDIENCE)
    monkeypatch.setattr(settings, "entra_issuer", _ISSUER)
    monkeypatch.setattr(auth, "_signing_key", lambda _token: rsa_key.public_key())


@pytest.fixture
def registry() -> Iterator[None]:
    """`acme` registered in the shared test schema's registry, and removed afterwards."""
    asyncio.run(migrated_db_or_skip())

    async def _write(sql: str) -> None:
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute(sql)

    forget_registered_tenants()
    asyncio.run(_write("INSERT INTO tenants (id) VALUES ('acme') ON CONFLICT (id) DO NOTHING"))
    yield
    asyncio.run(_write("DELETE FROM tenants WHERE id = 'acme'"))
    forget_registered_tenants()


def test_with_tenancy_off_the_roles_are_not_read(rsa_key: Any) -> None:
    """A token without a tenant role is the default tenant's; a deployment that never opted in."""
    assert validate_token(_token(rsa_key, roles=["bench"])).tenant == DEFAULT_TENANT
    assert validate_token(_token(rsa_key, roles=["tenant:acme"])).tenant == DEFAULT_TENANT


def test_with_tenancy_on_the_token_names_exactly_one(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `tenant:<id>` role is the tenant; none or two is refused as a tenant error."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    assert validate_token(_token(rsa_key, roles=["bench", "tenant:acme"])).tenant == "acme"
    for roles in ([], ["bench"], ["tenant:acme", "tenant:globex"], ["tenant:Not A Slug"]):
        with pytest.raises(auth.TenantRefused):
            validate_token(_token(rsa_key, roles=roles))
    assert issubclass(auth.TenantRefused, AuthError)


def _probe_app() -> FastAPI:
    """The real app plus a route reporting the tenant as the request and the database see it."""
    app = create_app()

    @app.get("/__tenant_probe")
    async def probe(principal: Principal = Depends(require_principal)) -> dict[str, str]:
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute("SELECT coalesce(current_setting('app.tenant', true), '')")
            row = await cursor.fetchone()
        return {
            "principal": principal.tenant,
            "ambient": current_tenant(),
            "database": "" if row is None else str(row[0]),
        }

    # In the dev posture the bundled UI is mounted at `/` and would answer first.
    app.router.routes.insert(0, app.router.routes.pop())
    return app


def _get(rsa_key: Any, roles: list[str]) -> Any:
    client = TestClient(_probe_app())
    return client.get(
        "/__tenant_probe", headers={"Authorization": f"Bearer {_token(rsa_key, roles=roles)}"}
    )


@pytest.mark.usefixtures("registry")
def test_a_request_carries_its_tenant_to_the_database(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route sees the principal's tenant as the ambient one and as `app.tenant`."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_tenant_id", "t")
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    response = _get(rsa_key, ["tenant:acme"])
    assert response.status_code == 200, response.text
    assert response.json() == {"principal": "acme", "ambient": "acme", "database": "acme"}


@pytest.mark.usefixtures("registry")
def test_a_token_with_no_usable_tenant_is_a_403_before_any_route(
    rsa_key: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token with no tenant, two, or one nobody registered is authenticated and refused."""
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "entra_tenant_id", "t")
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    for roles in ([], ["tenant:acme", "tenant:globex"], ["tenant:unregistered"]):
        response = _get(rsa_key, roles)
        assert response.status_code == 403, (roles, response.text)
        assert response.json() == {"detail": "no tenant"}


def test_the_dev_principal_needs_a_process_tenant_when_tenancy_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no identity provider the stand-in acts for the process's tenant, or is refused."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    assert TestClient(_probe_app()).get("/__tenant_probe").status_code == 403


def test_with_tenancy_off_a_request_acts_for_the_default_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged deployment: no registry read, no setting written, tenant `default`."""
    monkeypatch.setattr(settings, "entra_required", False)
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    response = TestClient(_probe_app()).get("/__tenant_probe")
    assert response.status_code == 200
    assert response.json()["principal"] == "default"
    assert response.json()["ambient"] == "default"
    assert response.json()["database"] == ""
