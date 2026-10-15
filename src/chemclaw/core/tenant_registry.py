"""The tenants an operator registered.

A tenant exists because a row says so (`python -m chemclaw.cli.tenants add <id>`), never because a
token named it: a `tenant:<id>` role for an id nobody registered is refused at the front door. The
table is global and read-only for the runtime role; registering is an operator's act under the
owning principal.

A tenant registered for a release (`--deployment`) is served by that release alone, so two releases
that share a database cannot answer for each other's tenants. One registered for none is served by
any.

Invariant: tenants are never removed (their rows would be orphaned), but one can move to another
release, so a positive answer is remembered for `tenant_registry_cache_seconds` and no longer.
"""

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import DEFAULT_TENANT, TenantError, bound_tenant

_T = TypeVar("_T")

#: tenant -> when its positive answer stops being believed (monotonic seconds).
_KNOWN: dict[str, float] = {}


class TenantSweepError(TenantError):
    """A sweep over the registered tenants would cover more of them than it is allowed to."""


async def is_registered(tenant: str) -> bool:
    """Whether `tenant` is in the registry and served by this release."""
    now = time.monotonic()
    if _KNOWN.get(tenant, 0.0) > now:
        return True
    _KNOWN.pop(tenant, None)
    async with db.connection(settings.postgres_dsn, operation="tenant_registry") as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM tenants WHERE id = %s AND (deployment_id IS NULL OR deployment_id = %s)",
            (tenant, settings.deployment_id),
        )
        found = await cursor.fetchone() is not None
    if found:
        _KNOWN[tenant] = now + settings.tenant_registry_cache_seconds
    return found


async def registered_tenants() -> list[str]:
    """This release's registered tenants, in id order: what a sweep that acts for each walks."""
    async with db.connection(settings.postgres_dsn, operation="tenant_registry") as conn:
        cursor = await conn.execute(
            "SELECT id FROM tenants WHERE deployment_id IS NULL OR deployment_id = %s ORDER BY id",
            (settings.deployment_id,),
        )
        return [str(row[0]) for row in await cursor.fetchall()]


async def for_each_tenant(work: Callable[[str], Awaitable[_T]]) -> dict[str, _T]:
    """Run `work(tenant)` once per tenant of this release, acting for that tenant, and collect.

    With tenancy off there is one tenant, `default`, and nothing is bound. With it on, work that
    binds no tenant reads and writes nothing, so a process-wide reading (a backlog gauge, the set of
    embedding models in use, a re-embed) must walk the registry; this is the one way to do it.

    Raises:
        TenantSweepError: more tenants are registered than `tenancy_sweep_max_tenants`. The sweep
            refuses rather than cover some of them and report the result as the whole.
    """
    if not settings.tenancy_enabled:
        return {DEFAULT_TENANT: await work(DEFAULT_TENANT)}
    tenants = await registered_tenants()
    if len(tenants) > settings.tenancy_sweep_max_tenants:
        raise TenantSweepError(
            f"{len(tenants)} tenants are registered and a sweep over them is capped at "
            f"{settings.tenancy_sweep_max_tenants} (CHEMCLAW_TENANCY_SWEEP_MAX_TENANTS)"
        )
    results: dict[str, _T] = {}
    for tenant in tenants:
        with bound_tenant(tenant):
            results[tenant] = await work(tenant)
    return results


def forget_registered_tenants() -> None:
    """Drop the process's memory of positive answers; for tests that rebuild the registry."""
    _KNOWN.clear()
