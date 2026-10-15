"""What Postgres itself refuses, as a role that is neither a superuser nor `BYPASSRLS`.

The suite's own connection is a superuser and no policy constrains it, so every assertion here runs
as the role `tenancy_database` creates. Each test states one property of the policy rather than of
a store: reads, updates and deletes reach only the connection's tenant, an insert for another
tenant is refused by `WITH CHECK`, a connection with no tenant reads nothing, two tenants can hold
the same hash-made key, the vector columns are filtered with their row, and the owner of a table is
subject to the policy only because the table is forced.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest

from chemclaw.core.db import connect
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database


@pytest.fixture
async def database() -> AsyncIterator[TenancyDatabase]:
    """A private schema with the policies on and two tenants registered."""
    async with tenancy_database() as db:
        await enable_tenancy(db, "acme", "globex")
        yield db


@asynccontextmanager
async def acting_as(
    database: TenancyDatabase, tenant: str | None
) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """A connection as the application role for `tenant`; `None` sets nothing at all."""
    conn = await connect(database.app_dsn)
    try:
        if tenant is not None:
            await conn.execute("SELECT set_config('app.tenant', %s, false)", (tenant,))
            await conn.commit()
        yield conn
    finally:
        await conn.close()


async def _as_owner(database: TenancyDatabase, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """Rows as the suite's superuser, who sees every tenant's."""
    async with await connect(database.owner_dsn) as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall() if cursor.description else []
        await conn.commit()
        return rows


async def _two_tenants_preferences(database: TenancyDatabase) -> None:
    """One person's preference in each tenant, written by the tenants themselves."""
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            await conn.execute(
                "INSERT INTO user_preferences (owner, key, value) VALUES ('p', 'unit', %s)",
                (f"value of {tenant}",),
            )
            await conn.commit()


async def test_a_tenant_reads_only_its_own_rows(database: TenancyDatabase) -> None:
    """The same owner and key in two tenants are two rows, and each sees its own."""
    await _two_tenants_preferences(database)
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            cursor = await conn.execute("SELECT value, tenant_id FROM user_preferences")
            assert await cursor.fetchall() == [(f"value of {tenant}", tenant)]


async def test_no_tenant_reads_nothing_and_writes_nothing(database: TenancyDatabase) -> None:
    """Unset and empty are both closed: no row is visible and an insert is refused."""
    await _two_tenants_preferences(database)
    for tenant in (None, ""):
        async with acting_as(database, tenant) as conn:
            cursor = await conn.execute("SELECT count(*) FROM user_preferences")
            assert await cursor.fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(
                    "INSERT INTO user_preferences (owner, key, value) VALUES ('p', 'k', 'v')"
                )


async def test_an_update_or_delete_without_a_where_touches_only_its_tenant(
    database: TenancyDatabase,
) -> None:
    """A store that forgot its filter still cannot change another tenant's rows."""
    await _two_tenants_preferences(database)
    async with acting_as(database, "acme") as conn:
        updated = await conn.execute("UPDATE user_preferences SET value = 'rewritten'")
        assert updated.rowcount == 1
        await conn.commit()
    assert sorted(await _as_owner(database, "SELECT tenant_id, value FROM user_preferences")) == [
        ("acme", "rewritten"),
        ("globex", "value of globex"),
    ]
    async with acting_as(database, "acme") as conn:
        deleted = await conn.execute("DELETE FROM user_preferences")
        assert deleted.rowcount == 1
        await conn.commit()
    assert await _as_owner(database, "SELECT tenant_id FROM user_preferences") == [("globex",)]


async def test_a_row_cannot_be_written_for_or_moved_to_another_tenant(
    database: TenancyDatabase,
) -> None:
    """`WITH CHECK` refuses an insert naming another tenant, and an update handing a row over."""
    await _two_tenants_preferences(database)
    async with acting_as(database, "acme") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                "INSERT INTO user_preferences (owner, key, value, tenant_id) "
                "VALUES ('p', 'forged', 'v', 'globex')"
            )
    async with acting_as(database, "acme") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("UPDATE user_preferences SET tenant_id = 'globex'")
    assert len(await _as_owner(database, "SELECT 1 FROM user_preferences")) == 2


async def test_the_default_gives_an_insert_the_connections_tenant(
    database: TenancyDatabase,
) -> None:
    """No store names the tenant: the column fills itself from the connection."""
    async with acting_as(database, "globex") as conn:
        await conn.execute(
            "INSERT INTO user_preferences (owner, key, value) VALUES ('p', 'k', 'v')"
        )
        await conn.commit()
    assert await _as_owner(database, "SELECT tenant_id FROM user_preferences") == [("globex",)]


async def test_a_conflicting_key_in_another_tenant_changes_nothing(
    database: TenancyDatabase,
) -> None:
    """An upsert onto a minted key another tenant holds can neither read nor overwrite that row.

    Whether Postgres answers with an error or with nothing is its business; what the policy owes is
    that the other tenant's row is the same afterwards.
    """
    await _two_tenants_preferences(database)
    async with acting_as(database, "acme") as conn:
        await conn.execute("DELETE FROM user_preferences")
        await conn.commit()
    async with acting_as(database, "acme") as conn:
        try:
            await conn.execute(
                "INSERT INTO user_preferences (owner, key, value) VALUES ('p', 'unit', 'mine') "
                "ON CONFLICT (owner, key) DO UPDATE SET value = EXCLUDED.value"
            )
            await conn.commit()
        except psycopg.Error:
            await conn.rollback()
    rows = await _as_owner(database, "SELECT tenant_id, value FROM user_preferences")
    assert ("globex", "value of globex") in rows


async def test_two_tenants_can_hold_the_same_hash_made_key(database: TenancyDatabase) -> None:
    """A calculation of the same structure is a row in each tenant; neither overwrites the other.

    The key is a hash of the inputs, which two tenants can both produce: the policy hides the
    other's row from the conflict check, so the key must carry the tenant for both inserts to land.
    """
    upsert = (
        "INSERT INTO calculation_results (key, calc_type, calc_version, input_hash, params_hash, "
        "result) VALUES ('k1', 't', 'v', 'i', 'p', %s::jsonb) "
        "ON CONFLICT ON CONSTRAINT calculation_results_pkey DO UPDATE SET result = EXCLUDED.result"
    )
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            await conn.execute(upsert, (f'{{"by": "{tenant}"}}',))
            await conn.commit()
    async with acting_as(database, "acme") as conn:
        await conn.execute(upsert, ('{"by": "acme again"}',))
        await conn.commit()
    assert sorted(
        await _as_owner(database, "SELECT tenant_id, result->>'by' FROM calculation_results")
    ) == [("acme", "acme again"), ("globex", "globex")]


_NOTE_VECTOR = "array_fill(%s::real, ARRAY[%s])::vector"


async def _nearest(conn: psycopg.AsyncConnection[Any], column: str, width: int, near: float) -> Any:
    """The `note_id` nearest to a vector of `near`s, over whatever the connection may see."""
    cursor = await conn.execute(
        f"SELECT note_id FROM note_index WHERE {column} IS NOT NULL "
        f"ORDER BY {column} <-> {_NOTE_VECTOR} LIMIT 1",
        (near, width),
    )
    row = await cursor.fetchone()
    return None if row is None else row[0]


@pytest.mark.parametrize(("column", "width"), [("embedding", 1536), ("embedding_768", 768)])
async def test_a_vector_search_sees_only_its_tenants_rows(
    database: TenancyDatabase, column: str, width: int
) -> None:
    """The policy filters the row, so the nearest neighbour is the nearest *of this tenant's*.

    The other tenant's vector is exactly on the query point; if the column escaped the policy it
    would win. The sibling column a changed embedding width adds (`embedding_<N>`) is the same row,
    and is checked the same way. The note slug is the same in both tenants.
    """
    if column != "embedding":
        await _as_owner(database, "SELECT ensure_embedding_width(%s)", (width,))
    for tenant, level in (("acme", 0.9), ("globex", 0.1)):
        async with acting_as(database, tenant) as conn:
            await conn.execute(
                f"INSERT INTO note_index (note_id, {column}) "
                f"VALUES ('shared-slug', {_NOTE_VECTOR})",
                (level, width),
            )
            await conn.execute(
                f"INSERT INTO note_index (note_id, {column}) "
                f"VALUES ('only-{tenant}', {_NOTE_VECTOR})",
                (level + 0.01, width),
            )
            await conn.commit()
    async with acting_as(database, "acme") as conn:
        # Globex's vectors sit at 0.1, which is exactly this query's point.
        cursor = await conn.execute(
            f"SELECT note_id FROM note_index ORDER BY {column} <-> {_NOTE_VECTOR}", (0.1, width)
        )
        assert [row[0] for row in await cursor.fetchall()] == ["shared-slug", "only-acme"]
        assert await _nearest(conn, column, width, 0.1) == "shared-slug"
    async with acting_as(database, "globex") as conn:
        cursor = await conn.execute(
            f"SELECT note_id FROM note_index ORDER BY {column} <-> {_NOTE_VECTOR}", (0.9, width)
        )
        assert [row[0] for row in await cursor.fetchall()] == ["only-globex", "shared-slug"]
    async with acting_as(database, None) as conn:
        assert await _nearest(conn, column, width, 0.1) is None


async def test_document_chunks_and_knowledge_follow_the_same_policy(
    database: TenancyDatabase,
) -> None:
    """The other vector store and the knowledge tables are tenant-scoped rows too."""
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            await conn.execute(
                "INSERT INTO document_chunks (doc_id, ordinal, content, chunking_key, embedding) "
                f"VALUES (%s, 0, %s, 'c', {_NOTE_VECTOR})",
                (f"doc-{tenant}", f"text of {tenant}", 0.5, 1536),
            )
            await conn.execute(
                "INSERT INTO kg_notes (id, type, content_sha) VALUES ('n1', 'reaction', %s)",
                (f"sha256:{tenant}",),
            )
            await conn.commit()
    async with acting_as(database, "acme") as conn:
        cursor = await conn.execute(
            f"SELECT content FROM document_chunks ORDER BY embedding <-> {_NOTE_VECTOR} LIMIT 5",
            (0.5, 1536),
        )
        assert await cursor.fetchall() == [("text of acme",)]
        cursor = await conn.execute("SELECT content_sha, tenant_id FROM kg_notes")
        assert await cursor.fetchall() == [("sha256:acme", "acme")]


async def test_a_superuser_and_a_bypassrls_role_see_every_tenant(database: TenancyDatabase) -> None:
    """The control that says why the start-up guard exists: the policy does not bind these."""
    await _two_tenants_preferences(database)
    assert len(await _as_owner(database, "SELECT 1 FROM user_preferences")) == 2
    async with await connect(database.owner_dsn) as conn:
        await conn.execute(f'ALTER ROLE "{database.role}" BYPASSRLS')
        await conn.commit()
    async with acting_as(database, "acme") as conn:
        cursor = await conn.execute("SELECT count(*) FROM user_preferences")
        assert await cursor.fetchone() == (2,)


async def test_the_owner_of_a_table_is_subject_to_the_policy_because_it_is_forced(
    database: TenancyDatabase,
) -> None:
    """In a single-principal deployment the runtime role owns its tables; FORCE is what binds it."""
    await _two_tenants_preferences(database)
    async with await connect(database.owner_dsn) as conn:
        await conn.execute(f'ALTER TABLE user_preferences OWNER TO "{database.role}"')
        await conn.commit()
    async with acting_as(database, "acme") as conn:
        cursor = await conn.execute("SELECT tenant_id FROM user_preferences")
        assert await cursor.fetchall() == [("acme",)]
    async with await connect(database.owner_dsn) as conn:
        await conn.execute("ALTER TABLE user_preferences NO FORCE ROW LEVEL SECURITY")
        await conn.commit()
    async with acting_as(database, "acme") as conn:
        cursor = await conn.execute("SELECT count(*) FROM user_preferences")
        assert await cursor.fetchone() == (2,), "without FORCE the owner would see both tenants"


async def test_the_audit_trail_and_a_cached_results_provenance_are_per_tenant(
    database: TenancyDatabase,
) -> None:
    """Who ran what, and who computed a cached result, are each tenant's own.

    The calculation itself is the same physics for everyone; the row says that *somebody here* asked
    for it, which is what a competitor must not learn, so the cache is scoped and so is the trail.
    """
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            await conn.execute(
                "INSERT INTO audit_events (correlation_id, actor, tool, arguments, outcome, "
                "latency_ms) VALUES ('c', %s, 'xtb', '{}', 'ok', 1.0)",
                (f"chemist of {tenant}",),
            )
            await conn.execute(
                "INSERT INTO calculation_results (key, calc_type, calc_version, input_hash, "
                "params_hash, result, provenance) VALUES ('k', 't', 'v', 'i', 'p', '{}', %s)",
                (f"computed for {tenant}",),
            )
            await conn.commit()
    for tenant in ("acme", "globex"):
        async with acting_as(database, tenant) as conn:
            cursor = await conn.execute("SELECT actor FROM audit_events")
            assert await cursor.fetchall() == [(f"chemist of {tenant}",)]
            cursor = await conn.execute("SELECT provenance FROM calculation_results")
            assert await cursor.fetchall() == [(f"computed for {tenant}",)]
