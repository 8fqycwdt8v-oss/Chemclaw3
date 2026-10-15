"""The ambient tenant, and the connection that carries it.

`core/tenancy.py` holds who the work acts for; `core/db.connection()` writes it into `app.tenant`
on every borrow and the pool's reset hook clears it on return. The properties that matter and that
are cheap to lose: a pooled connection never carries a previous tenant, work with no tenant reads
nothing, a `rollback()` inside the block cannot undo the binding, and with tenancy off nothing is
sent at all.
"""

from typing import Any

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import (
    DEFAULT_TENANT,
    TenantError,
    bound_tenant,
    current_tenant,
    reset_current_tenant,
    set_current_tenant,
    tenant_from_roles,
)
from tests.tenancy_db import enable_tenancy, tenancy_database


@pytest.fixture
def tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant, as a deployment that has switched it on."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")


def test_with_tenancy_off_everything_is_the_default_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bound tenant is ignored while tenancy is off: a single-tenant deployment has one."""
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    with bound_tenant("acme"):
        assert current_tenant() == DEFAULT_TENANT
    assert current_tenant() == DEFAULT_TENANT


@pytest.mark.usefixtures("tenancy_on")
def test_with_tenancy_on_work_that_bound_no_tenant_acts_for_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound, else the process's, else `''` (which no row has)."""
    assert current_tenant() == ""
    with bound_tenant("acme"):
        assert current_tenant() == "acme"
        with bound_tenant(None):  # an activity with no header does not mask the outer binding
            assert current_tenant() == "acme"
    assert current_tenant() == ""
    monkeypatch.setattr(settings, "tenancy_process_tenant", "globex")
    assert current_tenant() == "globex"
    with bound_tenant("acme"):
        assert current_tenant() == "acme"


@pytest.mark.usefixtures("tenancy_on")
def test_a_tenant_is_a_slug_and_binding_is_undone() -> None:
    """A malformed id is refused where it is bound, and a reset restores what was there."""
    for bad in ("Acme", "a b", "-x", "x" * 64, "a/b", "a'; DROP TABLE t; --"):
        with pytest.raises(TenantError):
            set_current_tenant(bad)
    outer = set_current_tenant("acme")
    inner = set_current_tenant("globex")
    reset_current_tenant(inner)
    assert current_tenant() == "acme"
    reset_current_tenant(outer)
    assert current_tenant() == ""


def test_a_token_names_exactly_one_tenant() -> None:
    """One `tenant:<id>` role makes a tenant; none, two or a malformed one is refused."""
    assert tenant_from_roles({"tenant:acme", "chemist"}) == "acme"
    assert tenant_from_roles(["tenant:acme", "tenant:acme"]) == "acme"
    for roles in (set(), {"chemist"}, {"tenant:acme", "tenant:globex"}, {"tenant:Bad Id"}):
        with pytest.raises(TenantError):
            tenant_from_roles(roles)


async def _scalar(conn: Any, sql: str) -> Any:
    """The one value `sql` returns."""
    row = await (await conn.execute(sql)).fetchone()
    assert row is not None
    return row[0]


async def _show(conn: Any) -> str | None:
    """What this connection's `app.tenant` reads as; `None` when it was never set."""
    value = await _scalar(conn, "SELECT current_setting('app.tenant', true)")
    return None if value is None else str(value)


@pytest.mark.usefixtures("tenancy_on")
async def test_a_borrowed_connection_carries_the_bound_tenant_and_a_returned_one_none() -> None:
    """The pool has one connection, so the second borrow is the first one's connection again.

    The second borrow runs with tenancy off, which binds nothing, so what it reads is what the pool
    left on the connection: the reset hook's work, not the checkout's.
    """
    async with tenancy_database() as database, db.pooling():
        with bound_tenant("acme"):
            async with db.connection(database.app_dsn, pool_max_size=1) as first:
                assert await _show(first) == "acme"
                backend = await _scalar(first, "SELECT pg_backend_pid()")
        settings.tenancy_enabled = False
        try:
            async with db.connection(database.app_dsn, pool_max_size=1) as second:
                again = await _scalar(second, "SELECT pg_backend_pid()")
                assert again == backend, "the pool did not hand back the same connection"
                assert await _show(second) in (None, "")
        finally:
            settings.tenancy_enabled = True


@pytest.mark.usefixtures("tenancy_on")
async def test_a_rollback_inside_the_block_does_not_undo_the_tenant() -> None:
    """The setting is written in autocommit, so the borrower's own rollback cannot revert it."""
    async with tenancy_database() as database, db.pooling():
        with bound_tenant("acme"):
            async with db.connection(database.app_dsn) as conn:
                await conn.execute("SELECT 1")
                await conn.rollback()
                assert await _show(conn) == "acme"


@pytest.mark.usefixtures("tenancy_on")
async def test_a_dedicated_connection_is_bound_too() -> None:
    """Outside `pooling()` (a script, a test) the borrow still carries the tenant."""
    async with tenancy_database() as database:
        with bound_tenant("globex"):
            async with db.connection(database.app_dsn) as conn:
                assert await _show(conn) == "globex"


@pytest.mark.usefixtures("tenancy_on")
async def test_work_with_no_tenant_reads_nothing_and_writes_nothing() -> None:
    """Fail closed: `''` matches no row, and an insert is refused by `WITH CHECK`."""
    import psycopg

    async with tenancy_database() as database:
        await enable_tenancy(database, "acme")
        with bound_tenant("acme"):
            async with db.connection(database.app_dsn) as conn:
                await conn.execute(
                    "INSERT INTO user_preferences (owner, key, value) VALUES ('u', 'k', '{}')"
                )
        async with db.connection(database.app_dsn) as conn:
            cursor = await conn.execute("SELECT count(*) FROM user_preferences")
            assert (await cursor.fetchone()) == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.connection(database.app_dsn) as conn:
                await conn.execute(
                    "INSERT INTO user_preferences (owner, key, value) VALUES ('u', 'k2', '{}')"
                )
        with bound_tenant("acme"):
            async with db.connection(database.app_dsn) as conn:
                cursor = await conn.execute("SELECT count(*) FROM user_preferences")
                assert (await cursor.fetchone()) == (1,)


async def test_with_tenancy_off_a_borrow_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default deployment pays no round trip and sets no setting."""
    sent: list[str] = []

    async def spy(conn: object, statement: str, params: object = None) -> None:
        sent.append(statement)

    monkeypatch.setattr(db, "_autocommitted", spy)
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    async with tenancy_database() as database, db.pooling():
        with bound_tenant("acme"):
            async with db.connection(database.owner_dsn) as conn:
                assert await _show(conn) in (None, "")
    assert sent == []


def test_a_workflow_id_names_its_tenant_so_identical_requests_do_not_join_across_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default tenant's ids are unchanged; another tenant's are its own, and read back as such.

    Temporal's namespace is shared, so a deterministic id is how two tenants asking the same thing
    would join one run and read each other's result.
    """
    from chemclaw.core.tenancy import owns_workflow, scope_workflow_id

    monkeypatch.setattr(settings, "tenancy_enabled", False)
    assert scope_workflow_id("bo-start-abc") == "bo-start-abc"
    assert owns_workflow("acme~bo-start-abc")  # nothing is refused while tenancy is off

    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    with bound_tenant("default"):
        assert scope_workflow_id("bo-start-abc") == "bo-start-abc"
        assert owns_workflow("bo-start-abc")
        assert not owns_workflow("acme~bo-start-abc")
    with bound_tenant("acme"):
        mine = scope_workflow_id("bo-start-abc")
        assert mine == "acme~bo-start-abc"
        assert owns_workflow(mine)
        assert owns_workflow(f"{mine}:approval")  # an id derived from a run's own
        assert not owns_workflow("bo-start-abc")
        assert not owns_workflow("globex~bo-start-abc")
        assert not owns_workflow("acmeish~bo-start-abc")
    # No tenant bound: no id can be minted, and no run is anyone's.
    with pytest.raises(TenantError):
        scope_workflow_id("bo-start-abc")
    assert not owns_workflow("bo-start-abc")


def test_a_turn_acts_for_its_senders_tenant_whatever_context_it_inherited(
    tenancy_on: None,
) -> None:
    """The turn binds the tenant it is given, and gives back the one it found."""
    from chemclaw.agent.turn_usage import TurnUsage
    from chemclaw.api.runner import _turn_ambient

    with bound_tenant("globex"):
        with _turn_ambient("s", "alice", frozenset(), False, "c", TurnUsage(), [], None, "acme"):
            assert current_tenant() == "acme"
        assert current_tenant() == "globex"
        with _turn_ambient("s", "alice", frozenset(), False, "c", TurnUsage(), [], None):
            assert current_tenant() == "globex", "no tenant given keeps the caller's"
