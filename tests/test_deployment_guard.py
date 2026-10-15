"""Which release owns the database, and whether tenancy is in the state a release assumes.

`judge` is a pure function over what the database says, so the rules are tabulated here without a
database. The rest drives the real migrator, the real startup check and the registry against a
private schema, as the role the application connects as.
"""

import asyncio
import logging
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from chemclaw.api.app import create_app
from chemclaw.cli.tenants import add_tenant, assign_tenant, list_tenants
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.deployment import (
    DeploymentRefused,
    Facts,
    Tenant,
    judge,
    read_facts,
    verify_deployment,
)
from chemclaw.core.migrate import MigrationError, migrate
from chemclaw.core.tenant_registry import forget_registered_tenants, is_registered
from chemclaw.durable.serve import serve_worker
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database

A = "ns-a/release-a"
B = "ns-b/release-b"

#: Tenancy switched on and enforced everywhere, keys widened, a role the policies bind.
_ENFORCED = {"scoped_tables": 56, "enforced_tables": 56, "keys_widened": True}


def _judge(
    facts: Facts,
    *,
    own: str = A,
    enabled: bool = False,
    kg: bool = False,
    hnsw: str = "relaxed_order",
) -> list[str]:
    """`judge` with the arguments a test is not about filled in."""
    return judge(
        facts, own=own, enabled=enabled, knowledge_in_postgres=kg, hnsw_iterative_scan=hnsw
    )


def _facts(stamp: str | None = A, *tenants: Tenant, **enforced: Any) -> Facts:
    """Facts as the database could say them."""
    return Facts(stamp=stamp, tenants=tenants, **enforced)


# --- the rules, without a database ---------------------------------------------------------------


@pytest.mark.parametrize("stamp", [None, A])
def test_an_unstamped_database_and_the_same_release_are_served(stamp: str | None) -> None:
    """The first release to arrive, and every later start of it, find nothing to refuse."""
    assert _judge(_facts(stamp)) == []


def test_a_release_with_no_identity_is_not_judged_on_one() -> None:
    """Nothing is configured, so nothing is compared; the chart always configures it."""
    assert _judge(_facts(B), own="") == []


def test_a_second_release_is_refused_naming_both_and_the_remedy() -> None:
    """The message carries both ids, the setting that fixes it and the sharing recipe."""
    (problem,) = _judge(_facts(B))
    assert B in problem and A in problem
    assert "CHEMCLAW_POSTGRES_DSN" in problem
    assert "CHEMCLAW_TENANCY_ENABLED" in problem and f"DEPLOYMENT={A}" in problem


@pytest.mark.parametrize(
    ("enabled", "enforced", "owns_a_tenant", "admitted"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
    ids=["all three", "tenancy off", "not enforced", "no tenant of its own"],
)
def test_a_second_release_is_admitted_only_with_tenancy_enforced_and_a_tenant_of_its_own(
    enabled: bool, enforced: bool, owns_a_tenant: bool, admitted: bool
) -> None:
    """Sharing a database is allowed when, and only when, the policies keep the releases apart."""
    tenants = [Tenant("theirs", B)] + ([Tenant("mine", A)] if owns_a_tenant else [])
    facts = _facts(B, *tenants, **(_ENFORCED if enforced else {}))
    problems = _judge(facts, enabled=enabled)
    refused = [p for p in problems if "already belongs to release" in p]
    assert (refused == []) is admitted


def test_a_tenant_left_to_every_release_is_refused_on_both_sides() -> None:
    """Sharing needs distinct tenants: one that names no release would be served by both."""
    tenants = (Tenant("shared", None), Tenant("theirs", B), Tenant("mine", A))
    for stamp in (A, B):
        problems = _judge(_facts(stamp, *tenants, **_ENFORCED), enabled=True)
        assert any("registered for no release" in p and "shared" in p for p in problems), stamp
    assert (
        _judge(_facts(B, Tenant("theirs", B), Tenant("mine", A), **_ENFORCED), enabled=True) == []
    )


def test_tenancy_on_over_policies_that_are_not_enforced_is_refused() -> None:
    """The switch is a migration; a process told it is on over a database where it is not, stops."""
    (problem,) = _judge(_facts(A, scoped_tables=56, enforced_tables=3), enabled=True)
    assert "3 of 56" in problem and "db-migrate" in problem


def test_policies_enforced_while_tenancy_is_off_is_refused() -> None:
    """Every query would return nothing, which looks like an empty system rather than a mistake."""
    (problem,) = _judge(_facts(A, **_ENFORCED), enabled=False)
    assert "CHEMCLAW_TENANCY_ENABLED=true" in problem


def test_tenancy_on_with_keys_not_widened_is_refused() -> None:
    """Two tenants could still collide on a hash the old key admits once."""
    facts = _facts(A, scoped_tables=56, enforced_tables=56, keys_widened=False)
    (problem,) = _judge(facts, enabled=True)
    assert "tenant_id" in problem


def test_a_role_the_policies_do_not_bind_is_refused_only_when_tenancy_is_on() -> None:
    """A superuser or BYPASSRLS role reads every tenant's rows, but is no fault with tenancy off."""
    facts = _facts(A, role_bypasses=True, **_ENFORCED)
    (problem,) = _judge(facts, enabled=True)
    assert "BYPASSRLS" in problem
    assert _judge(_facts(A, role_bypasses=True), enabled=False) == []


def test_a_second_tenant_needs_tenant_safe_knowledge_and_vector_search() -> None:
    """With two served tenants the knowledge graph and the vector scan must be tenant-safe."""
    facts = _facts(A, Tenant("acme", None), Tenant("globex", None), **_ENFORCED)
    git = _judge(facts, enabled=True, kg=False, hnsw="relaxed_order")
    assert len(git) == 1 and "kg_store=git" in git[0]
    off = _judge(facts, enabled=True, kg=True, hnsw="off")
    assert len(off) == 1 and "hnsw_iterative_scan" in off[0]
    assert _judge(facts, enabled=True, kg=True, hnsw="relaxed_order") == []


def test_another_releases_tenants_do_not_count_as_this_ones() -> None:
    """One served tenant is not a second tenant, whatever the other release has registered."""
    facts = _facts(A, Tenant("mine", A), Tenant("theirs", B), Tenant("theirs2", B), **_ENFORCED)
    assert _judge(facts, enabled=True, kg=False, hnsw="off") == []


# --- the migrator ---------------------------------------------------------------------------------


async def _stamp(database: TenancyDatabase) -> list[str]:
    """The release the database says owns it."""
    async with await connect(database.owner_dsn) as conn:
        cursor = await conn.execute("SELECT deployment_id FROM deployment_id")
        return [str(row[0]) for row in await cursor.fetchall()]


async def test_the_first_release_stamps_the_database_and_a_second_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrate hook refuses a release that is not the owner before any pod rolls."""
    async with tenancy_database() as database:
        assert await _stamp(database) == [], "a release with no identity stamps nothing"
        monkeypatch.setattr(settings, "deployment_id", A)
        await migrate(database.owner_dsn)
        await migrate(database.owner_dsn)
        assert await _stamp(database) == [A]

        monkeypatch.setattr(settings, "deployment_id", B)
        with pytest.raises(MigrationError) as raised:
            await migrate(database.owner_dsn)
        assert A in str(raised.value) and B in str(raised.value)
        assert await _stamp(database) == [A], "a refused release leaves the stamp alone"


async def _own_default_tenant(
    monkeypatch: pytest.MonkeyPatch, database: TenancyDatabase, release: str
) -> None:
    """Give the tenant every database starts with to `release`, as an operator does to share it."""
    monkeypatch.setattr(settings, "postgres_migration_dsn", database.owner_dsn)
    assert await assign_tenant("default", release) is True
    assert await assign_tenant("nobody", release) is False


async def test_a_second_release_migrates_once_tenancy_and_a_tenant_each_are_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recipe the refusal names works: tenancy on, a tenant each, none left to both."""
    async with tenancy_database() as database:
        monkeypatch.setattr(settings, "deployment_id", A)
        monkeypatch.setattr(settings, "tenancy_enabled", True)
        monkeypatch.setattr(settings, "postgres_migration_dsn", database.owner_dsn)
        monkeypatch.setattr(settings, "postgres_dsn", database.owner_dsn)
        await migrate(database.owner_dsn)

        monkeypatch.setattr(settings, "deployment_id", B)
        with pytest.raises(MigrationError, match="already belongs to release"):
            await migrate(database.owner_dsn)

        assert await add_tenant("globex", B) is True
        assert await add_tenant("globex", A) is False, "a registered tenant keeps its release"
        with pytest.raises(MigrationError, match="registered for no release"):
            await migrate(database.owner_dsn)

        await _own_default_tenant(monkeypatch, database, A)
        await migrate(database.owner_dsn)
        assert await _stamp(database) == [A]
        assert [(t, r) for t, _, r in await list_tenants()] == [("default", A), ("globex", B)]

        await add_tenant("shared")
        with pytest.raises(MigrationError, match=r"\['shared'\] are registered for no release"):
            await migrate(database.owner_dsn)


# --- the startup check ----------------------------------------------------------------------------


async def _verify(
    monkeypatch: pytest.MonkeyPatch, dsn: str, *, own: str = A, enabled: bool = False
) -> None:
    """`verify_deployment` as a process configured this way."""
    monkeypatch.setattr(settings, "postgres_dsn", dsn)
    monkeypatch.setattr(settings, "deployment_id", own)
    monkeypatch.setattr(settings, "tenancy_enabled", enabled)
    await verify_deployment()


async def test_the_startup_check_refuses_a_release_that_does_not_own_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read as the application's role, which can see the stamp and cannot write it."""
    async with tenancy_database() as database:
        monkeypatch.setattr(settings, "deployment_id", A)
        await migrate(database.owner_dsn)
        await _verify(monkeypatch, database.app_dsn, own=A)
        with pytest.raises(DeploymentRefused) as raised:
            await _verify(monkeypatch, database.app_dsn, own=B)
        assert A in str(raised.value) and B in str(raised.value)


async def test_a_second_release_starts_when_the_database_keeps_the_tenants_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both releases pass once tenancy is enforced and each has its own tenant, as a bound role."""
    async with tenancy_database() as database:
        monkeypatch.setattr(settings, "deployment_id", A)
        await migrate(database.owner_dsn)
        async with await connect(database.owner_dsn) as conn:
            await conn.execute(
                "INSERT INTO tenants (id, deployment_id) VALUES ('globex', %s)", (B,)
            )
            await conn.commit()
        await _own_default_tenant(monkeypatch, database, A)
        await enable_tenancy(database)
        await _verify(monkeypatch, database.app_dsn, own=A, enabled=True)
        await _verify(monkeypatch, database.app_dsn, own=B, enabled=True)
        with pytest.raises(DeploymentRefused, match="BYPASSRLS"):
            await _verify(monkeypatch, database.owner_dsn, own=B, enabled=True)
        with pytest.raises(DeploymentRefused, match="CHEMCLAW_TENANCY_ENABLED=true"):
            await _verify(monkeypatch, database.app_dsn, own=B, enabled=False)


async def test_the_startup_check_reads_a_database_that_predates_the_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check also runs before the migration that adds what it reads; absent is a reading."""
    async with tenancy_database() as database:
        async with await connect(database.owner_dsn) as conn:
            await conn.execute("DROP TABLE deployment_id")
            await conn.execute("ALTER TABLE tenants DROP COLUMN deployment_id")
            facts = await read_facts(conn)
            await conn.commit()
        assert [(t.tenant_id, t.deployment_id) for t in facts.tenants] == [("default", None)]
        assert facts.scoped_tables > 50
        # As the owner: the isolation search path falls through to `public`, whose tables the
        # application's role is not granted in the shared test database.
        monkeypatch.setattr(settings, "session_store", "postgres")
        await _verify(monkeypatch, database.owner_dsn, own="")


async def test_an_unreachable_database_is_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An outage is readiness's to report; refusing to start would turn it into a crash loop."""
    caplog.set_level(logging.WARNING)
    await _verify(monkeypatch, "postgresql://nobody:nothing@127.0.0.1:1/none", own=A)
    assert any(r.__dict__.get("event") == "deployment.unchecked" for r in caplog.records)


async def test_nothing_is_read_when_nothing_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """No identity, tenancy off, sessions in memory: no database for an answer to be about."""
    monkeypatch.setattr(settings, "session_store", "memory")
    await _verify(monkeypatch, "postgresql://nobody:nothing@127.0.0.1:1/none", own="")


# --- the registry ---------------------------------------------------------------------------------


async def test_a_release_serves_its_own_tenants_and_those_registered_for_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant registered for another release is unknown here, so its token gets a 403."""
    async with tenancy_database() as database:
        async with await connect(database.owner_dsn) as conn:
            await conn.execute(
                "INSERT INTO tenants (id, deployment_id) VALUES ('mine', %s), ('theirs', %s), "
                "('open', NULL)",
                (A, B),
            )
            await conn.commit()
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "deployment_id", A)
        forget_registered_tenants()
        try:
            assert [await is_registered(t) for t in ("mine", "theirs", "open", "nobody")] == [
                True,
                False,
                True,
                False,
            ]
        finally:
            forget_registered_tenants()


# --- the entrypoints ------------------------------------------------------------------------------


async def _refuse() -> None:
    """A guard that finds a contradiction."""
    raise DeploymentRefused("this database belongs to another release")


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connector is attached: the test is about the guard, not the tools."""
    return []


def test_the_front_door_does_not_start_on_a_database_it_may_not_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan runs the guard before it serves; a refusal fails startup."""
    monkeypatch.setattr("chemclaw.api.app.verify_deployment", _refuse)
    app = create_app(connector_factory=_no_connectors)
    with pytest.raises(DeploymentRefused), TestClient(app):
        pytest.fail("the app served on a database it may not serve")


class _NeverPolled:
    """A worker that records whether it was ever started."""

    is_running = False
    client = None

    def __init__(self) -> None:
        """Not started."""
        self.started = False

    async def run(self) -> None:
        """Record the start."""
        self.started = True

    async def shutdown(self) -> None:
        """Nothing to drain."""


def test_a_worker_does_not_poll_on_a_database_it_may_not_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`serve_worker` runs the guard before the first poll."""
    monkeypatch.setattr("chemclaw.durable.serve.verify_deployment", _refuse)
    monkeypatch.setattr(settings, "worker_metrics_port", 0)
    worker = _NeverPolled()

    async def serve() -> None:
        await serve_worker(cast(Any, worker), component="test-worker")

    with pytest.raises(DeploymentRefused):
        asyncio.run(serve())
    assert worker.started is False


async def test_a_tenant_handed_to_another_release_stops_being_served_within_the_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tenant-assign` reaches running pods: a positive answer is believed for a short time only."""
    async with tenancy_database() as database:
        async with await connect(database.owner_dsn) as conn:
            await conn.execute("INSERT INTO tenants (id, deployment_id) VALUES ('t', %s)", (A,))
            await conn.commit()
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "postgres_migration_dsn", database.owner_dsn)
        monkeypatch.setattr(settings, "deployment_id", A)
        monkeypatch.setattr(settings, "tenant_registry_cache_seconds", 0.2)
        forget_registered_tenants()
        assert await is_registered("t") is True
        assert await assign_tenant("t", B) is True
        assert await is_registered("t") is True, "still believed inside the window"
        await asyncio.sleep(0.3)
        assert await is_registered("t") is False, "the pod went on serving a tenant it had lost"
        forget_registered_tenants()


async def test_one_table_losing_its_policy_is_noticed() -> None:
    """The guard counts the tables that carry a tenant, not the policies that exist."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        async with await connect(database.owner_dsn) as conn:
            whole = await read_facts(conn)
            await conn.execute("DROP POLICY tenant_isolation ON session_messages")
            await conn.commit()
            broken = await read_facts(conn)
        assert whole.scoped_tables > 50 and whole.enforced_tables == whole.scoped_tables
        assert broken.scoped_tables == whole.scoped_tables
        assert broken.enforced_tables == whole.enforced_tables - 1
        problems = _judge(broken, own="", enabled=True)
        assert any(f"{broken.enforced_tables} of {broken.scoped_tables}" in p for p in problems)
