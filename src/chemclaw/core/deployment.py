"""Whether this release may serve this database, and whether tenancy is in the state it assumes.

Two releases on one database share every table, and a chart cannot see that. The database says it
itself: a one-row stamp (`deployment_id`) holds the identity of the release that migrated it, and
the migrate hook, the front door and every worker compare it with their own on start. A different
identity is refused unless tenancy is on, enforced, and this release owns a tenant. The same read
checks what a tenancy deployment assumes and nothing else enforces: the policies are on, the
runtime role is not exempt from them, a second tenant has somewhere to keep its knowledge.

Invariants: `judge` is pure and names every problem it finds, both release ids included; an
unreachable database is not a refusal (readiness reports an outage), a contradiction is.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event

logger = logging.getLogger(__name__)

#: A table every widened key is checked on: `chemclaw_apply_tenancy` widens all of them or none.
_KEY_WITNESS = "calculation_results"


class DeploymentRefused(RuntimeError):
    """This release must not serve this database as configured; the message says what to do."""


@dataclass(frozen=True)
class Tenant:
    """A registered tenant and the release that serves it (`None`: any)."""

    tenant_id: str
    deployment_id: str | None


@dataclass(frozen=True)
class Facts:
    """What the database says, read in one transaction."""

    #: The stamped release identity, or `None` when none is stamped (or the table is absent).
    stamp: str | None = None
    tenants: Sequence[Tenant] = field(default_factory=tuple)
    #: Tables carrying the tenant policy, and how many of them enforce it (enabled and forced).
    scoped_tables: int = 0
    enforced_tables: int = 0
    #: Whether the connecting role is a superuser or has `BYPASSRLS`.
    role_bypasses: bool = False
    #: Whether the keys two tenants could both produce carry `tenant_id`.
    keys_widened: bool = False


_EXISTS = (
    "SELECT to_regclass('deployment_id') IS NOT NULL, to_regclass('tenants') IS NOT NULL, "
    "EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('tenants') "
    "AND attname = 'deployment_id' AND NOT attisdropped)"
)
_STAMP = "SELECT deployment_id FROM deployment_id"
_TENANTS = "SELECT id FROM tenants ORDER BY id"
_OWNED_TENANTS = "SELECT id, deployment_id FROM tenants ORDER BY id"
_POLICIES = (
    "SELECT count(*), count(*) FILTER (WHERE c.relrowsecurity AND c.relforcerowsecurity) "
    "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
    "WHERE p.polname = 'tenant_isolation' AND to_regclass(c.relname::text) = c.oid"
)
_ROLE = "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
_WIDENED = (
    "SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid "
    "AND a.attnum = ANY (c.conkey) WHERE c.contype = 'p' AND c.conrelid = to_regclass(%s) "
    "AND a.attname = 'tenant_id')"
)


async def read_facts(conn: psycopg.AsyncConnection[TupleRow]) -> Facts:
    """The stamp, the registry, the policies' state and the role, from `conn`.

    Tolerant of a database that predates what it asks about: the guard also runs before the
    migration that adds the stamp, the registry and the policies, and finding them absent is a
    reading, not an error.
    """
    cursor = await conn.execute(_EXISTS)
    present = await cursor.fetchone()
    has_stamp, has_registry, has_owner = (
        (bool(present[0]), bool(present[1]), bool(present[2])) if present else (False,) * 3
    )
    stamp: str | None = None
    if has_stamp:
        rows = await (await conn.execute(_STAMP)).fetchall()
        stamp = str(rows[0][0]) if rows else None
    tenants: list[Tenant] = []
    if has_registry:
        # The owning release is a column of 132; a database at 131 has the registry without it.
        rows = await (await conn.execute(_OWNED_TENANTS if has_owner else _TENANTS)).fetchall()
        tenants = [
            Tenant(str(r[0]), str(r[1]) if has_owner and r[1] is not None else None) for r in rows
        ]
    counts = await (await conn.execute(_POLICIES)).fetchone()
    widened = await (await conn.execute(_WIDENED, (_KEY_WITNESS,))).fetchone()
    role = await (await conn.execute(_ROLE)).fetchone()
    return Facts(
        stamp=stamp,
        tenants=tuple(tenants),
        scoped_tables=int(counts[0]) if counts else 0,
        enforced_tables=int(counts[1]) if counts else 0,
        role_bypasses=bool(role and role[0]),
        keys_widened=bool(widened and widened[0]),
    )


def unowned_tenants_problem(facts: Facts) -> str | None:
    """Why tenants that belong to no release cannot stay so on a database two releases share."""
    unowned = [t.tenant_id for t in facts.tenants if t.deployment_id is None]
    if not unowned:
        return None
    return (
        f"tenants {unowned} are registered for no release, so every release that shares this "
        "database would serve them. Give each to one release, under the owning principal: "
        "`make tenant-assign TENANT=<tenant> DEPLOYMENT=<release>`"
    )


def release_problem(facts: Facts, *, own: str, enabled: bool, enforced: bool) -> str | None:
    """Why a release other than the one that stamped this database may not use it, or `None`.

    Allowed only when tenancy is on and enforced, this release owns a tenant of its own and no
    tenant is left to every release.
    """
    if not own or facts.stamp is None or facts.stamp == own:
        return None
    ours = [t for t in facts.tenants if t.deployment_id == own]
    if enabled and enforced and ours:
        return unowned_tenants_problem(facts)
    return (
        f"this database already belongs to release {facts.stamp!r} and this release is {own!r}. "
        "Two releases that share a database share every table, and one's retention sweep prunes "
        "the other's live threads. Give this release a database of its own "
        "(CHEMCLAW_POSTGRES_DSN). To share one on purpose, turn tenancy on in both "
        "(CHEMCLAW_TENANCY_ENABLED=true, then run the migration), register this release's tenants "
        f"with `make tenant-add TENANT=<id> DEPLOYMENT={own}` and give every existing tenant to "
        "one release with `make tenant-assign TENANT=<id> DEPLOYMENT=<release>`. If "
        f"{facts.stamp!r} is this same release under a new name, re-stamp it under the owning "
        f"principal: UPDATE deployment_id SET deployment_id = '{own}'"
    )


def judge(
    facts: Facts,
    *,
    own: str,
    enabled: bool,
    knowledge_in_postgres: bool,
    hnsw_iterative_scan: str,
) -> list[str]:
    """Every reason this release may not serve this database as configured; empty when it may.

    Args:
        facts: What the database says.
        own: This release's identity (`settings.deployment_id`), or `""`.
        enabled: `settings.tenancy_enabled`.
        knowledge_in_postgres: Whether the knowledge graph is held in Postgres, where it is
            tenant-scoped, rather than in the repository tree every tenant would share.
        hnsw_iterative_scan: `settings.hnsw_iterative_scan`.
    """
    problems: list[str] = []
    enforced = facts.scoped_tables > 0 and facts.enforced_tables == facts.scoped_tables
    if (refused := release_problem(facts, own=own, enabled=enabled, enforced=enforced)) is not None:
        problems.append(refused)
    elif enabled and any(t.deployment_id not in (None, own) for t in facts.tenants):
        # This release is the stamped one, or none is: another release's tenants share its database.
        if (unowned := unowned_tenants_problem(facts)) is not None:
            problems.append(unowned)
    if enabled and not enforced:
        problems.append(
            f"tenancy_enabled is set but row-level security is enforced on "
            f"{facts.enforced_tables} of {facts.scoped_tables} tenant-scoped tables, so no tenant "
            "is kept from another's rows. Run the migration with tenancy enabled (`make "
            "db-migrate`, or the chart's pre-upgrade hook), which enables and forces the policies"
        )
    if not enabled and facts.enforced_tables:
        problems.append(
            f"the database enforces tenancy on {facts.enforced_tables} tables but tenancy_enabled "
            "is false, so every query here would return nothing. Set "
            "CHEMCLAW_TENANCY_ENABLED=true, or switch it off by running the migration without it "
            "(refused while more than one tenant is registered)"
        )
    if enabled and enforced and not facts.keys_widened:
        problems.append(
            "tenancy is enforced but the keys two tenants could both produce do not yet carry "
            "tenant_id; run the migration with tenancy enabled"
        )
    if enabled and facts.role_bypasses:
        problems.append(
            "the database role this process connects as is a superuser or has BYPASSRLS, which "
            "row-level security does not bind: every tenant's rows would be readable by every "
            "other. Connect as a role that is neither (the chart's `chemclaw_app`)"
        )
    served = [t for t in facts.tenants if t.deployment_id in (None, own)]
    if enabled and len(served) > 1:
        if not knowledge_in_postgres:
            problems.append(
                f"{len(served)} tenants are registered for this release but the knowledge graph is "
                "one repository for the whole deployment (kg_store=git), which every tenant would "
                "read and write. Hold the knowledge in Postgres first, or register one tenant"
            )
        if hnsw_iterative_scan == "off":
            problems.append(
                f"{len(served)} tenants are registered but hnsw_iterative_scan is off: a vector "
                "search is filtered by tenant after the index returns its nearest rows, so a small "
                "tenant would get fewer results than it asks for. Set "
                "CHEMCLAW_HNSW_ITERATIVE_SCAN=relaxed_order"
            )
    return problems


async def verify_deployment() -> None:
    """Refuse to start when `judge` finds a contradiction; say so and carry on when it cannot look.

    Called on entering a process's pool (front door lifespan, worker entrypoints). Nothing is read
    where no identity is configured, tenancy is off and sessions are in memory: there is then no
    database for the answer to be about.
    """
    if not (
        settings.deployment_id or settings.tenancy_enabled or settings.session_store == "postgres"
    ):
        return
    try:
        async with db.connection(settings.postgres_dsn, operation="deployment_guard") as conn:
            facts = await read_facts(conn)
    except ConnectionError:
        log_event(
            logger,
            "deployment.unchecked",
            "the deployment guard could not read the database, so release identity and tenancy "
            "were not checked; readiness reports the outage",
            level=logging.WARNING,
        )
        return
    problems = judge(
        facts,
        own=settings.deployment_id,
        enabled=settings.tenancy_enabled,
        knowledge_in_postgres=str(settings.kg_store) == "postgres",
        hnsw_iterative_scan=settings.hnsw_iterative_scan,
    )
    if problems:
        raise DeploymentRefused("; ".join(problems))
