"""The frames a turn's holder relays to another replica are a tenant's answer text.

The requests that address a turn are global, because the replica holding it polls those of every
turn it holds. The frames are scoped: the holder writes them bound to the tenant the request was
asked in, and only that tenant's asker can take them.
"""

import pytest

from chemclaw.agent.turn_remotes import TurnRemotes
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenancy import bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from tests.tenancy_db import enable_tenancy, tenancy_database


@pytest.fixture(autouse=True)
def _tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()


async def test_only_the_tenant_that_asked_can_take_the_frames_a_holder_relays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder with no tenant of its own relays for acme's request; globex's poll gets nothing."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        async with await connect(database.owner_dsn) as conn:
            await conn.execute(
                "INSERT INTO session_owners (session_id, owner, tenant_id) "
                "VALUES ('s-a', 'alice', 'acme')"
            )
            await conn.commit()
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "session_store_dsn", "")
        remotes = TurnRemotes()

        with bound_tenant("acme"):
            request_id = await remotes.ask("s-a", "holder-1", "watch", "alice", 30.0)

        # The holder's loop serves every tenant and binds none: it reads the request's tenant.
        (request,) = await remotes.pending([("s-a", "holder-1")])
        assert request.id == request_id and request.tenant == "acme"
        await remotes.relay(request_id, [{"event": "token", "data": "secret answer"}, None], "acme")

        with bound_tenant("globex"):
            _, stolen = await remotes.poll(request_id, 30.0)
        assert stolen == [], "another tenant took a frame it has no policy to see"

        async with await connect(database.owner_dsn) as conn:
            cursor = await conn.execute("SELECT tenant_id FROM session_turn_frames ORDER BY seq")
            assert [row[0] for row in await cursor.fetchall()] == ["acme", "acme"]

        with bound_tenant("acme"):
            _, frames = await remotes.poll(request_id, 30.0)
        assert frames == [{"event": "token", "data": "secret answer"}, None]
