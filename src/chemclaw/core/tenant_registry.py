"""The tenants an operator registered.

A tenant exists because a row says so (`python -m chemclaw.cli.tenants add <id>`), never because a
token named it: a `tenant:<id>` role for an id nobody registered is refused at the front door. The
table is global and read-only for the runtime role; registering is an operator's act under the
owning principal.

A tenant registered for a release (`--deployment`) is served by that release alone, so two releases
that share a database cannot answer for each other's tenants. One registered for none is served by
any.

Invariant: tenants are never removed (their rows would be orphaned), so a positive answer is cached
for the life of the process and only an unknown id costs a query.
"""

from chemclaw.core import db
from chemclaw.core.config import settings

_KNOWN: set[str] = set()


async def is_registered(tenant: str) -> bool:
    """Whether `tenant` is in the registry."""
    if tenant in _KNOWN:
        return True
    async with db.connection(settings.postgres_dsn, operation="tenant_registry") as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM tenants WHERE id = %s AND (deployment_id IS NULL OR deployment_id = %s)",
            (tenant, settings.deployment_id),
        )
        found = await cursor.fetchone() is not None
    if found:
        _KNOWN.add(tenant)
    return found


async def registered_tenants() -> list[str]:
    """This release's registered tenants, in id order: what a sweep that acts for each walks."""
    async with db.connection(settings.postgres_dsn, operation="tenant_registry") as conn:
        cursor = await conn.execute(
            "SELECT id FROM tenants WHERE deployment_id IS NULL OR deployment_id = %s ORDER BY id",
            (settings.deployment_id,),
        )
        return [str(row[0]) for row in await cursor.fetchall()]


def forget_registered_tenants() -> None:
    """Drop the process's cache of positive answers; for tests that rebuild the registry."""
    _KNOWN.clear()
