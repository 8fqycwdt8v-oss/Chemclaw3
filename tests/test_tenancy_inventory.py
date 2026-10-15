"""Every table is tenant-scoped with a policy, or global for a stated reason.

The partition is read off a freshly migrated schema, never transcribed from the migration: a table
a later migration adds without `tenant_id`, a policy and a default fails here unless somebody wrote
down why it is global. The second half exercises the switch (`chemclaw_apply_tenancy`) in a private
schema, so the shared test schema keeps its policies disabled for every other test.
"""

import re
from pathlib import Path

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migrate
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database

# Global tables, each with the reason it carries no tenant. A table is here or it is scoped.
_GLOBAL: dict[str, str] = {
    "schema_migrations": "the migration ledger: it describes the schema, which has no tenant",
    "tenants": "the registry that defines the tenancy",
    "deployment_id": "the release that owns the database: a fact about the database, not a row",
    "session_turns": "the fleet's concurrent-turn ceiling counts leases across tenants",
    "session_turn_remotes": "the relay between replicas polls requests across tenants: leases",
    "session_turn_frames": "output in transit between replicas, keyed by a relay id",
    "request_buckets": "the per-principal request budget is the deployment's; one row per person",
    "audit_anchors": "retired and empty; nothing writes it",
}

# Scoped tables whose primary key does not take `tenant_id`, with why a second tenant cannot
# collide on it. Every other scoped table is in `_KEYED` and must carry it once tenancy is on.
_MINTED = (
    "a key this system mints (a session, turn, job, exhibit, design, effect, request or "
    "plan id), or one that begins with a data source, whose name is unique in a deployment"
)

_KEYED = {
    "artifact_blobs",
    "bo_campaigns",
    "budget_usage",
    "calculation_artifacts",
    "calculation_claims",
    "calculation_results",
    "composed_workflows",
    "corpus_molecules",
    "measurements",
    "molecule_fingerprints",
    "note_index",
    "observations",
    "structures",
    "tool_result_blobs",
    "user_preferences",
}

# Constraints, not primary keys, that identify a row the same way and are widened with the keys.
_KEYED_CONSTRAINTS = {
    ("predictions", "predictions_identity"),
    ("result_publications", "result_publications_identity"),
}


async def _rows(dsn: str, sql: str) -> list[tuple[object, ...]]:
    async with await connect(dsn) as conn:
        cursor = await conn.execute(sql)
        return [tuple(row) for row in await cursor.fetchall()]


# LangGraph's own tables are not created by a migration and are global (a thread id is reached only
# after the session has been resolved through a scoped table); `tenancy_database` makes them so the
# routes that delete a session have something to sweep.
_TABLES = (
    "SELECT c.relname FROM pg_class c WHERE c.relkind = 'r' AND c.relname NOT LIKE 'checkpoint%' "
    "AND c.relnamespace = current_schema()::regnamespace ORDER BY 1"
)


async def test_every_table_is_scoped_with_a_policy_or_global_for_a_stated_reason() -> None:
    """A table carries `tenant_id`, a `tenant_isolation` policy and a tenant default, or is global.

    The default is what lets an INSERT that names no tenant take the connection's, so a scoped table
    without it would write every row for `default` and be refused by the policy under any other
    tenant.
    """
    async with tenancy_database() as database:
        tables = {str(row[0]) for row in await _rows(database.owner_dsn, _TABLES)}
        scoped_columns = {
            str(row[0]): str(row[1])
            for row in await _rows(
                database.owner_dsn,
                "SELECT table_name, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND column_name = 'tenant_id'",
            )
        }
        policies = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in await _rows(
                database.owner_dsn,
                "SELECT c.relname, pg_get_expr(p.polqual, p.polrelid), "
                "pg_get_expr(p.polwithcheck, p.polrelid) FROM pg_policy p "
                "JOIN pg_class c ON c.oid = p.polrelid WHERE p.polname = 'tenant_isolation' "
                "AND c.relnamespace = current_schema()::regnamespace",
            )
        }
    assert len(tables) > 50, "the schema was not migrated; every assertion below would be vacuous"
    assert set(_GLOBAL) <= tables, f"global register names tables that do not exist: {_GLOBAL}"
    scoped = tables - set(_GLOBAL)
    assert set(scoped_columns) == scoped, (
        f"tables with a tenant_id column {sorted(set(scoped_columns) ^ scoped)} are not exactly "
        "the scoped ones: add the table to a migration's `chemclaw_scope_table` list, or to "
        "`_GLOBAL` above with the reason it has no tenant"
    )
    assert set(policies) == scoped, f"policy missing or stray: {sorted(set(policies) ^ scoped)}"
    for table in sorted(scoped):
        assert "app.tenant" in scoped_columns[table], f"{table}: the tenant default is wrong"
        using, check = policies[table]
        assert using == check == "(tenant_id = current_setting('app.tenant'::text, true))", (
            f"{table}: the policy is {using!r} / {check!r}"
        )


async def test_the_switch_widens_the_hash_keys_and_nothing_else() -> None:
    """Switching on puts `tenant_id` last in exactly the keys two tenants could both produce.

    Before it, every primary key is what it was (the previous image still writes); after it, the
    keyed tables end in `tenant_id`, the foreign keys that point at them follow, and the minted keys
    are untouched. Last, so a lookup by the old key still leads the index.
    """
    async with tenancy_database() as database:
        before = await _primary_keys(database)
        assert not any(
            "tenant_id" in cols for table, cols in before.items() if not table.startswith("kg_")
        )
        await enable_tenancy(database, "acme")
        after = await _primary_keys(database)
        for table, cols in after.items():
            if table in _KEYED:
                assert cols[-1] == "tenant_id" and cols[:-1] == before[table], (table, cols)
            else:
                assert cols == before[table], f"{table}: {_MINTED} should keep its key"
        constraints = {
            (str(r[0]), str(r[1])): str(r[2])
            for r in await _rows(
                database.owner_dsn,
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE contype = 'u'",
            )
        }
        for key in _KEYED_CONSTRAINTS:
            assert constraints[key].endswith(", tenant_id)"), constraints[key]
        foreign = await _rows(
            database.owner_dsn,
            "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid::regclass::text IN "
            "('artifact_blobs', 'bo_campaigns', 'tool_result_blobs')",
        )
        assert len(foreign) == 3
        assert all("tenant_id" in str(definition) for _, definition in foreign), foreign


async def _primary_keys(database: TenancyDatabase) -> dict[str, list[str]]:
    rows = await _rows(
        database.owner_dsn,
        "SELECT c.conrelid::regclass::text, array_agg(a.attname::text ORDER BY k.ord) "
        "FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(n, ord) "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n "
        "WHERE c.contype = 'p' AND c.connamespace = current_schema()::regnamespace "
        "GROUP BY c.conrelid",
    )
    return {str(table): list(cols) for table, cols in rows}  # type: ignore[call-overload]


def _constraints_named_in_src() -> set[str]:
    """Every constraint a statement in `src/` names with `ON CONSTRAINT`."""
    source = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    return {
        match
        for path in source.rglob("*.py")
        for match in re.findall(r"ON CONSTRAINT (\w+)", path.read_text("utf-8"))
    }


async def test_every_statement_that_names_a_key_by_constraint_names_one_that_exists() -> None:
    """`ON CONFLICT ON CONSTRAINT x` in `src/` names a constraint, before and after the switch.

    That is the point of naming it: the same statement works under the narrow key and the wide one.
    A renamed constraint would fail at the first write, in the one deployment that switched on.
    """
    named = _constraints_named_in_src()
    assert len(named) >= 17, f"the scan found only {sorted(named)}"
    async with tenancy_database() as database:
        for stage in ("narrow", "wide"):
            if stage == "wide":
                await enable_tenancy(database, "acme")
            present = {
                str(row[0])
                for row in await _rows(database.owner_dsn, "SELECT conname FROM pg_constraint")
            }
            assert named <= present, f"{stage} keys: missing {sorted(named - present)}"


async def test_switching_is_idempotent_and_off_is_refused_with_two_tenants() -> None:
    """A second `apply` changes nothing; off is refused with two tenants and allowed with one."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        async with await connect(database.owner_dsn) as conn:
            cursor = await conn.execute("SELECT chemclaw_apply_tenancy(true)")
            assert (await cursor.fetchone()) == (0,)
            cursor = await conn.execute("SELECT chemclaw_apply_tenancy(false)")
            row = await cursor.fetchone()
            assert row is not None and row[0] > 50
            await conn.execute("SELECT chemclaw_apply_tenancy(true)")
            await conn.execute("INSERT INTO tenants (id) VALUES ('globex')")
            with pytest.raises(psycopg.errors.RaiseException, match="switched off while 2"):
                await conn.execute("SELECT chemclaw_apply_tenancy(false)")


async def test_the_migrator_applies_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """`migrate()` enables and forces the policies when `tenancy_enabled`, else leaves them off."""
    async with tenancy_database() as database:
        flags = (
            "SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relforcerowsecurity "
            "AND relnamespace = current_schema()::regnamespace"
        )
        assert await _rows(database.owner_dsn, flags) == [(0,)]
        monkeypatch.setattr(settings, "tenancy_enabled", True)
        await migrate(database.owner_dsn)
        forced = (await _rows(database.owner_dsn, flags))[0][0]
        assert isinstance(forced, int) and forced > 50
        monkeypatch.setattr(settings, "tenancy_enabled", False)
        await migrate(database.owner_dsn)
        assert await _rows(database.owner_dsn, flags) == [(0,)]
