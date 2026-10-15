"""The note store with tenancy on: two tenants, one database, a role the policies bind.

Every assertion about isolation runs as the application role `tenancy_database` creates (neither a
superuser nor `BYPASSRLS`), with acme and globex registered; rows are read back as the owner, who
sees both. A tenant's note lands in its own tenant; the import and the comparison for one tenant
never read or write the other; work with no tenant bound cannot mirror a note and is counted; and
the commands refuse to run without `--tenant`. The deployment guard keeps a second tenant out of any
mode that still reads the repository tree.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chemclaw.cli.kg_import import main as import_main
from chemclaw.cli.validate_kg import main as validate_main
from chemclaw.core.config import settings
from chemclaw.core.db import connect, pooling
from chemclaw.core.deployment import Facts, Tenant, judge, knowledge_is_per_tenant
from chemclaw.core.metrics import METRICS
from chemclaw.core.tenancy import TenantError, bound_tenant
from chemclaw.core.tenant_registry import forget_registered_tenants
from chemclaw.durable.kg_compare import compare_knowledge_stores
from chemclaw.durable.schedules import planned_schedules, tenant_schedules
from chemclaw.kg.compare import compare_once
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.importer import import_notes
from chemclaw.kg.note import Note
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.record import record_note
from chemclaw.kg.store import GitNoteStore, NoteUnit
from tests.kg_repo import notes_repo, write_notes
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database

_FAILED = "chemclaw_kg_dual_write_failed_total"


def _note(note_id: str, body: str = "A note.") -> Note:
    return Note(id=note_id, type="playbook", created_by="agent", body=body)


@dataclass
class Deployment:
    """The private schema, a notes directory and a clone with its writer."""

    database: TenancyDatabase
    notes: Path
    clone: Path
    writer: GitNoteWriter

    async def owner_rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        """Rows as the superuser, who sees every tenant's."""
        async with await connect(self.database.owner_dsn) as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()

    async def bodies(self, tenant: str) -> dict[str, str]:
        """Each of `tenant`'s notes and its body."""
        rows = await self.owner_rows(
            "SELECT id, body FROM kg_notes WHERE tenant_id = %s ORDER BY id", tenant
        )
        return {str(i): str(b) for i, b in rows}


@pytest.fixture
async def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Deployment]:
    """Tenancy on, acme and globex registered, `kg_store=dual`, the pool on the bound role."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    monkeypatch.setattr(settings, "kg_store", "dual")
    monkeypatch.setattr(settings, "kg_compare_settle_seconds", 0.0)
    forget_registered_tenants()
    clone = notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    invalidate_cache()
    notes = tmp_path / "notes"
    notes.mkdir()
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "postgres_migration_dsn", database.app_dsn)
        async with pooling():
            yield Deployment(
                database,
                notes,
                clone,
                GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin"),
            )


async def _main(entry: Any, *argv: str) -> int:
    return await asyncio.to_thread(entry, list(argv))


# --- Dual-write ----------------------------------------------------------------------------------


async def test_a_dual_write_lands_each_tenants_note_in_its_own_tenant(
    deployment: Deployment,
) -> None:
    for tenant in ("acme", "globex"):
        with bound_tenant(tenant):
            await record_note(_note(f"{tenant}-note", f"Written for {tenant}."), deployment.writer)
            await record_note(_note("shared-id", f"{tenant}'s version."), deployment.writer)
    assert await deployment.bodies("acme") == {
        "acme-note": "Written for acme.",
        "shared-id": "acme's version.",
    }
    assert await deployment.bodies("globex") == {
        "globex-note": "Written for globex.",
        "shared-id": "globex's version.",
    }


async def test_a_mirror_with_no_tenant_is_refused_and_counted_and_the_chemists_write_stands(
    deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    """Failing closed means the Postgres copy is not made, never that the tree write is undone."""
    failed = METRICS.value(_FAILED)
    with caplog.at_level(logging.ERROR):
        reference = await record_note(_note("headerless", "No tenant bound."), deployment.writer)
    assert reference
    assert (deployment.clone / "knowledge" / "playbook" / "headerless.md").exists()
    assert METRICS.value(_FAILED) == failed + 1
    assert any(
        "kg.dual_write.failed" in r.getMessage() and "TenantError" in r.getMessage()
        for r in caplog.records
    )
    assert await deployment.owner_rows("SELECT count(*) FROM kg_notes") == [(0,)]


# --- Import and comparison -----------------------------------------------------------------------


async def _seed_two_tenants(deployment: Deployment) -> None:
    """Acme holds `x` as the directory does; globex holds another `x` and a `y`."""
    write_notes(deployment.notes, _note("x", "The directory's x."))
    store = PostgresNoteStore()
    with bound_tenant("acme"):
        await store.put(NoteUnit(_note("x", "The directory's x.")))
    with bound_tenant("globex"):
        await store.put(NoteUnit(_note("x", "Globex's own x.")))
        await store.put(NoteUnit(_note("y", "Globex only.")))
    invalidate_cache()


async def test_an_import_for_one_tenant_neither_reads_nor_changes_the_other(
    deployment: Deployment,
) -> None:
    write_notes(deployment.notes, _note("x", "From the directory."), _note("z", "Also."))
    store = PostgresNoteStore()
    with bound_tenant("globex"):
        await store.put(NoteUnit(_note("x", "Globex's own x.")))
    async with await connect(deployment.database.owner_dsn) as conn:
        for tenant in ("acme", "globex"):
            await conn.execute(
                "INSERT INTO note_index (tenant_id, note_id, fingerprint) VALUES (%s, 'x', 'f')",
                (tenant,),
            )
        await conn.commit()
    with bound_tenant("acme"):
        summary = await import_notes(deployment.notes, tenant="acme")
    assert summary.tenant == "acme" and summary.created == 2 and summary.complete
    assert await deployment.bodies("acme") == {"x": "From the directory.", "z": "Also."}
    assert await deployment.bodies("globex") == {"x": "Globex's own x."}
    linked = await deployment.owner_rows(
        "SELECT tenant_id, kg_note_id FROM note_index WHERE note_id = 'x' ORDER BY tenant_id"
    )
    assert linked == [("acme", "x"), ("globex", None)]
    with bound_tenant("globex"), pytest.raises(TenantError):
        await import_notes(deployment.notes, tenant="acme")


async def test_a_comparison_and_a_repair_for_one_tenant_never_touch_the_other(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    await _seed_two_tenants(deployment)
    notes = str(deployment.notes)
    assert await _main(validate_main, "--compare", "--tenant", "acme", notes) == 0
    capsys.readouterr()
    assert await _main(validate_main, "--compare", "--json", "--tenant", "globex", notes) == 1
    report = json.loads(capsys.readouterr().out)["comparison"]
    assert {d["id"] for d in report["differences"]} == {"x", "y"}
    assert await _main(validate_main, "--compare", "--repair", "--tenant", "globex", notes) == 1
    # The repair put the directory's `x` into globex and left globex's `y` (nothing deletes).
    assert await deployment.bodies("globex") == {"x": "The directory's x.", "y": "Globex only."}
    assert await deployment.bodies("acme") == {"x": "The directory's x."}
    with bound_tenant("acme"):
        found = await compare_once(GitNoteStore(deployment.notes), PostgresNoteStore())
    assert found.clean


async def test_the_scheduled_comparison_acts_for_the_tenant_it_is_bound_to(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_two_tenants(deployment)
    monkeypatch.setattr(settings, "note_repo_dir", str(deployment.notes.parent))
    monkeypatch.setattr(settings, "knowledge_dir", deployment.notes.name)
    with bound_tenant("acme"):
        assert (await compare_knowledge_stores()).clean
    with bound_tenant("globex"):
        report = await compare_knowledge_stores()
    assert not report.clean and report.differing == ["x", "y"]
    with pytest.raises(TenantError):
        await compare_knowledge_stores()


def test_the_comparison_is_scheduled_once_per_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy expands every job that is not source-bound into `<id>@<tenant>` Schedules."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "kg_store", "dual")
    plan = tenant_schedules(planned_schedules(), ["acme", "globex"], strict=False)
    compare = {p.schedule_id: p.tenant for p in plan if p.schedule_id.startswith("kg-compare")}
    assert compare == {"kg-compare@acme": "acme", "kg-compare@globex": "globex"}


# --- The commands refuse without a tenant -------------------------------------------------------


async def test_the_commands_refuse_to_run_without_a_tenant_and_change_nothing(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    write_notes(deployment.notes, _note("x"))
    notes = str(deployment.notes)
    assert await _main(import_main, notes) == 2
    assert "--tenant" in capsys.readouterr().err
    assert await _main(validate_main, "--compare", notes) == 2
    assert "--tenant" in capsys.readouterr().err
    assert await _main(import_main, "--tenant", "nobody", notes) == 2
    assert "not a tenant registered" in capsys.readouterr().err
    assert await deployment.owner_rows("SELECT count(*) FROM kg_notes") == [(0,)]
    assert await _main(import_main, "--tenant", "acme", notes) == 0
    assert await deployment.bodies("acme") == {"x": "A note."}


async def test_with_tenancy_off_the_commands_take_the_one_tenant_and_refuse_another(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    write_notes(deployment.notes, _note("x"))
    assert await _main(import_main, "--tenant", "acme", str(deployment.notes)) == 2
    assert "tenancy is off" in capsys.readouterr().err


# --- The deployment guard ------------------------------------------------------------------------


def _facts(*tenants: str) -> Facts:
    return Facts(
        tenants=tuple(Tenant(t, None) for t in tenants),
        scoped_tables=1,
        enforced_tables=1,
        keys_widened=True,
    )


@pytest.mark.parametrize(("mode", "refused"), [("git", True), ("dual", True), ("postgres", False)])
def test_a_second_tenant_is_refused_in_every_mode_that_still_reads_the_tree(
    monkeypatch: pytest.MonkeyPatch, mode: str, refused: bool
) -> None:
    """Dual holds a tenant-scoped copy but reads the tree, which every tenant would read."""
    monkeypatch.setattr(settings, "kg_store", mode)
    problems = judge(
        _facts("acme", "globex"),
        own="",
        enabled=True,
        knowledge_in_postgres=knowledge_is_per_tenant(),
        hnsw_iterative_scan="relaxed_order",
    )
    assert any("one repository for the whole deployment" in p for p in problems) is refused
    one = judge(
        _facts("acme"),
        own="",
        enabled=True,
        knowledge_in_postgres=knowledge_is_per_tenant(),
        hnsw_iterative_scan="relaxed_order",
    )
    assert not any("one repository" in p for p in one)
