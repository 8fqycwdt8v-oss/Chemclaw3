"""The ambient tenant: whose rows the work in flight may read and write.

A request, an activity or a CLI acts for one tenant. A `contextvar` carries it as it carries the
actor (`identity_context`), and `core/db.connection()` writes it into the connection's
`app.tenant` setting, which the row-level-security policies compare with each row's `tenant_id`.

Invariants:
- With `tenancy_enabled` false the tenant is always `default` and no setting is written, so a
  single-tenant deployment behaves as it did before tenancy existed.
- With it true, work that bound no tenant and has no `tenancy_process_tenant` acts for `''`, which
  no row has: it reads nothing and every insert is refused. Failing closed is the default.
- A tenant id is a slug. A role `tenant:<id>` is the only way a token names one.
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from chemclaw.core.config import settings
from chemclaw.core.config.store import TENANT_ID_PATTERN
from chemclaw.core.errors import ChemclawError

#: The tenant of a single-tenant deployment, and the default of every `tenant_id` column.
DEFAULT_TENANT = "default"

#: The prefix of the app role that assigns a principal to a tenant (`tenant:acme`).
TENANT_ROLE_PREFIX = "tenant:"

#: The Postgres setting the policies read. A namespaced custom setting, so no server restart or
#: privilege is needed to set it.
TENANT_SETTING = "app.tenant"

#: The header a tenant crosses Temporal in.
TENANT_HEADER = "x-chemclaw-tenant"

#: Between a tenant and the rest of a workflow id. Not a character a tenant id can hold.
WORKFLOW_ID_SEPARATOR = "~"


class TenantError(ChemclawError):
    """A tenant id is malformed, or a principal does not name exactly one tenant.

    A `ChemclawError`, so retrying finds the same answer; `durable.retry._BAD_DATA_TYPES` lists it
    by name because Temporal matches that way.
    """


_current_tenant: ContextVar[str | None] = ContextVar("chemclaw_current_tenant", default=None)


def is_tenant_id(value: str) -> bool:
    """Whether `value` is a well-formed tenant id (a slug of at most 63 characters)."""
    return TENANT_ID_PATTERN.fullmatch(value) is not None


def require_tenant_id(value: str) -> str:
    """`value` if it is a tenant id; otherwise a `TenantError` saying what one looks like."""
    if not is_tenant_id(value):
        raise TenantError(
            f"{value!r} is not a tenant id: lowercase letters, digits, '_' and '-', starting with "
            "a letter or digit, at most 63 characters"
        )
    return value


def set_current_tenant(tenant: str | None) -> object:
    """Bind the tenant the work in flight acts for; returns a token for `reset_current_tenant`.

    `None` or blank binds nothing, so an activity with no tenant header does not mask the
    process's own.
    """
    value = (tenant or "").strip()
    return _current_tenant.set(require_tenant_id(value) if value else _current_tenant.get())


def reset_current_tenant(token: object) -> None:
    """Restore the previous tenant, undoing a `set_current_tenant` (teardown)."""
    _current_tenant.reset(token)  # type: ignore[arg-type]


@contextmanager
def bound_tenant(tenant: str | None) -> Iterator[None]:
    """Act for `tenant` inside the block, and for whoever was acting before it afterwards."""
    token = set_current_tenant(tenant)
    try:
        yield
    finally:
        reset_current_tenant(token)


def current_tenant() -> str:
    """The tenant a database borrow acts for right now.

    `default` while tenancy is off, whatever is bound. With it on: the bound tenant, else the
    process's, else `''` (no tenant, so no rows).
    """
    if not settings.tenancy_enabled:
        return DEFAULT_TENANT
    return _current_tenant.get() or settings.tenancy_process_tenant


def tenant_from_roles(roles: Iterable[str]) -> str:
    """The one tenant a token's roles name.

    Raises:
        TenantError: none, or more than one, `tenant:<id>` role, or a malformed id. A principal in
            two tenants would have to be refused by every query, so it is refused here.
    """
    prefix = len(TENANT_ROLE_PREFIX)
    named = sorted({r[prefix:] for r in roles if r.startswith(TENANT_ROLE_PREFIX)})
    if len(named) != 1:
        raise TenantError(
            "the token must carry exactly one tenant role "
            f"({TENANT_ROLE_PREFIX}<id>); it carries {len(named)}"
        )
    return require_tenant_id(named[0])


def namespace_component(base: str) -> str:
    """`base`, or `base@<tenant>` for any tenant but the default: a store namespace's first part.

    One component, because a store search matches a namespace by whole-component prefix: a second
    component under `base` would be listed by the default tenant's own search. The default tenant's
    namespaces are unchanged, so a deployment that never enabled tenancy keeps its stored items.
    """
    tenant = current_tenant()
    return base if tenant == DEFAULT_TENANT else f"{base}@{tenant}"


def scope_workflow_id(workflow_id: str) -> str:
    """`workflow_id` as the current tenant starts it: unchanged for `default`, else `<tenant>~<id>`.

    A deterministic id is how identical requests join one run, and Temporal's namespace is shared by
    every tenant: without the tenant in it, a second tenant's identical request would join the
    first's run and read its result. The default tenant's ids are unchanged, so a deployment that
    never enabled tenancy keeps every id (and every in-flight history) it has.

    Raises:
        TenantError: tenancy is on and no tenant is bound, so no id can be minted for anyone.
    """
    tenant = current_tenant()
    if tenant == DEFAULT_TENANT:
        return workflow_id
    if not tenant:
        raise TenantError("no tenant is bound, so no workflow id can be minted for one")
    return f"{tenant}{WORKFLOW_ID_SEPARATOR}{workflow_id}"


def owns_workflow(workflow_id: str) -> bool:
    """Whether the current tenant started `workflow_id`: the one place a run's tenant is read back.

    Temporal answers by id for anyone who asks, so a status or cancel route asks this first and
    treats a run that is not the caller's as unknown. Always true while tenancy is off.
    """
    if not settings.tenancy_enabled:
        return True
    tenant = current_tenant()
    if not tenant:
        return False
    owner, separator, _ = workflow_id.partition(WORKFLOW_ID_SEPARATOR)
    return owner == tenant if separator else tenant == DEFAULT_TENANT
