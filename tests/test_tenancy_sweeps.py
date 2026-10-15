"""Process-wide readings and operator sweeps under row-level security.

Work that binds no tenant reads nothing, which is right for a request and wrong for a gauge or a
re-embed that must cover everyone. These drive the registry walk (`for_each_tenant`) and each place
that depends on it, as a role the policies bind.
"""

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenancy import bound_tenant, current_tenant
from chemclaw.core.tenant_registry import (
    TenantSweepError,
    for_each_tenant,
    forget_registered_tenants,
)
from chemclaw.publish import outbox
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database


@pytest.fixture
def tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()


async def _seed_outbox(database: TenancyDatabase) -> None:
    """Two pending rows for acme and one for globex, to the same sink."""
    async with await connect(database.owner_dsn) as conn:
        for tenant, ref in (("acme", "c1"), ("acme", "c2"), ("globex", "c3")):
            await conn.execute(
                "INSERT INTO result_publications (sink, calc_ref, document, tenant_id) "
                "VALUES ('s', %s, '{}', %s)",
                (ref, tenant),
            )
        await conn.commit()


async def test_the_walk_acts_for_each_tenant_in_turn(
    tenancy_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call sees its own tenant bound, and the one that called sees its own again after."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        seen: list[str] = []

        async def work(tenant: str) -> str:
            seen.append(current_tenant())
            return tenant.upper()

        with bound_tenant("acme"):
            result = await for_each_tenant(work)
            assert current_tenant() == "acme"
        assert result == {"acme": "ACME", "default": "DEFAULT", "globex": "GLOBEX"}
        assert seen == ["acme", "default", "globex"]


async def test_the_walk_refuses_rather_than_cover_some_tenants(
    tenancy_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More tenants than the cap is an error, not a quiet partial answer."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "tenancy_sweep_max_tenants", 2)

        async def work(_tenant: str) -> None:
            return None

        with pytest.raises(TenantSweepError, match="capped at 2"):
            await for_each_tenant(work)


async def test_the_outbox_backlog_gauge_counts_every_tenants_rows(
    tenancy_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker binds no tenant, and a drain binds one: both must report the whole backlog."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        await _seed_outbox(database)
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(outbox, "enabled_names", lambda: ["s"])
        await outbox.refresh_backlog()
        assert outbox._PENDING_GAUGE["s"] == 3.0
        outbox._PENDING_GAUGE["s"] = 0.0
        with bound_tenant("globex"):
            await outbox.refresh_backlog()
        assert outbox._PENDING_GAUGE["s"] == 3.0


async def test_the_mixed_corpus_signal_sees_rows_a_tenantless_reader_cannot(
    tenancy_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tenant's notes under an old model are visible only by walking the tenants."""
    from chemclaw.core import db
    from chemclaw.core.vector_schema import (
        inspect_every_tenant,
        inspect_tables,
        live_embedding_keys,
    )

    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        async with await connect(database.owner_dsn) as conn:
            await conn.execute(
                "INSERT INTO note_index (note_id, embedding_key, tenant_id) "
                "VALUES ('n1', 'old:model:d3:m', 'globex')"
            )
            await conn.commit()
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        live = live_embedding_keys()
        async with db.connection(database.app_dsn) as conn:
            blind = await inspect_tables(conn, settings.embedding_dim, live)
        assert not any(state.mixed for state in blind), "a tenantless reader sees no rows at all"
        async with db.connection(database.app_dsn) as conn:
            seen = await inspect_every_tenant(conn, settings.embedding_dim, live)
        mixed = {state.table: state.foreign_keys for state in seen if state.mixed}
        assert mixed == {"note_index": ("old:model:d3:m",)}
