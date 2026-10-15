"""The Postgres note store with tenancy on: two tenants, one database, a role the policies bind.

`PostgresNoteStore` names its tenant in every statement; the policies compare each row with the
tenant the connection is bound to. These hold the two to the same answer: a store follows the tenant
the work acts for, refuses to name another, and a statement that names another tenant's rows still
finds none of them.
"""

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tenancy import TenantError, bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.store import NoteUnit
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database


def _note(note_id: str, body: str, **fields: object) -> Note:
    return Note(id=note_id, type="reaction", created_by="agent", body=body, **fields)  # type: ignore[arg-type]


@pytest.fixture
async def two_tenants(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A database where acme and globex hold the same ids, read through the bound role."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        store = PostgresNoteStore()
        with bound_tenant("acme"):
            await store.put(NoteUnit(_note("p", "acme only [[shared]]")))
            await store.put(NoteUnit(_note("a-only", "acme secret")))
        with bound_tenant("globex"):
            await store.put(NoteUnit(_note("p", "globex view [[shared]] [[x]]")))
            await store.put(
                NoteUnit(_note("q", "globex q", relations=[Relation(rel="uses", to="p")]))
            )
        yield database


async def test_a_default_store_follows_the_tenant_the_work_acts_for(
    two_tenants: TenancyDatabase,
) -> None:
    """The same store object answers from acme's notes for acme and globex's for globex."""
    store = PostgresNoteStore()
    with bound_tenant("acme"):
        assert [n.id for n in await store.notes()] == ["a-only", "p"]
        note = await store.get("p")
        assert note is not None and note.body.startswith("acme")
        assert await store.get("q") is None
    with bound_tenant("globex"):
        assert [n.id for n in await store.notes()] == ["p", "q"]
        note = await store.get("p")
        assert note is not None and note.body.startswith("globex")
        assert await store.get("a-only") is None


async def test_with_no_tenant_the_store_refuses_instead_of_reading_nothing(
    two_tenants: TenancyDatabase,
) -> None:
    """Unbound work gets a named error, not an empty corpus."""
    with pytest.raises(TenantError, match="none is named"):
        await PostgresNoteStore().notes()


async def test_a_store_for_one_tenant_refuses_work_acting_for_another(
    two_tenants: TenancyDatabase,
) -> None:
    """Naming acme while bound to globex is an error naming both, never a quiet empty answer."""
    acme = PostgresNoteStore("acme")
    with bound_tenant("acme"):
        assert [n.id for n in await acme.notes()] == ["a-only", "p"]
    with bound_tenant("globex"), pytest.raises(TenantError, match="'acme'.*'globex'"):
        await acme.notes()
    with bound_tenant("globex"), pytest.raises(TenantError):
        await acme.put(NoteUnit(_note("smuggled", "written across tenants")))


async def test_a_statement_that_names_another_tenants_rows_still_finds_none(
    two_tenants: TenancyDatabase,
) -> None:
    """The policy, not the store's own `WHERE`, is what keeps tenants apart."""
    with bound_tenant("globex"):
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute("SELECT id FROM kg_notes WHERE tenant_id = 'acme'")
            assert await cursor.fetchall() == []
            cursor = await conn.execute("SELECT count(*) FROM kg_notes")
            assert (await cursor.fetchone()) == (2,)
