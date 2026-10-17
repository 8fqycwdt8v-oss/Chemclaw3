"""Operator commands under tenancy: they name a tenant or they refuse.

Row-level security binds the owner too, so a command that names no tenant sees no row, deletes
nothing, and used to exit 0. Each command here is driven through its real `main` against a database
with two tenants: with none named it exits non-zero saying why and touches nothing; with one named
it acts for that tenant only; where it takes `--all-tenants` it acts for both.
"""

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from chemclaw.cli import erase_actor
from chemclaw.core import erasure_ledger
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.tenant_registry import forget_registered_tenants
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The erasure ledger an applied erasure needs before it runs."""
    path = tmp_path / "erasures.jsonl"
    monkeypatch.setattr(settings, "erasure_ledger_path", str(path))
    return path


@pytest.fixture
def tenancy_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenancy enabled with no process tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", True)
    monkeypatch.setattr(settings, "tenancy_process_tenant", "")
    forget_registered_tenants()


async def _in_two_tenants(
    monkeypatch: pytest.MonkeyPatch, scenario: Callable[[TenancyDatabase], Awaitable[None]]
) -> None:
    """Run `scenario` on a private schema with acme and globex registered, as the bound role."""
    async with tenancy_database() as database:
        await enable_tenancy(database, "acme", "globex")
        monkeypatch.setattr(settings, "postgres_dsn", database.app_dsn)
        monkeypatch.setattr(settings, "postgres_migration_dsn", database.app_dsn)
        await scenario(database)


async def _main(entry: Callable[[list[str]], int], *argv: str) -> int:
    """A command's real `main`, on its own thread because it runs its own event loop."""
    return await asyncio.to_thread(entry, list(argv))


async def _sessions(database: TenancyDatabase) -> dict[str, str]:
    """Every session row and its tenant, read as the superuser."""
    async with await connect(database.owner_dsn) as conn:
        cursor = await conn.execute("SELECT session_id, tenant_id FROM session_owners")
        return {str(row[0]): str(row[1]) for row in await cursor.fetchall()}


async def _seed_sessions(database: TenancyDatabase) -> None:
    """The same person owns a session in each tenant."""
    async with await connect(database.owner_dsn) as conn:
        for tenant in ("acme", "globex"):
            await conn.execute(
                "INSERT INTO session_owners (session_id, owner, tenant_id) "
                "VALUES (%s, 'alice', %s)",
                (f"s-{tenant}", tenant),
            )
        await conn.commit()


# --- erase_actor ----------------------------------------------------------------------------------


async def test_erasing_a_person_with_no_tenant_refuses_instead_of_reporting_success(
    tenancy_on: None,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The repro: `--apply` with no tenant printed all zeros and exited 0."""

    async def scenario(database: TenancyDatabase) -> None:
        await _seed_sessions(database)
        assert await _main(erase_actor.main, "alice", "--apply") == 1
        assert "--tenant" in capsys.readouterr().err
        assert set(await _sessions(database)) == {"s-acme", "s-globex"}

    await _in_two_tenants(monkeypatch, scenario)


async def test_erasing_a_person_acts_for_the_named_tenant_only(
    tenancy_on: None, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--tenant acme` removes acme's session and leaves the same person's globex session."""

    async def scenario(database: TenancyDatabase) -> None:
        await _seed_sessions(database)
        assert await _main(erase_actor.main, "alice", "--apply", "--tenant", "acme") == 0
        assert await _sessions(database) == {"s-globex": "globex"}

    await _in_two_tenants(monkeypatch, scenario)


async def test_erasing_a_person_can_cover_every_tenant(
    tenancy_on: None, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person who works in two tenants is erased from both by `--all-tenants`.

    Recorded once per tenant, because a replay re-applies each record in the tenant it names.
    """

    async def scenario(database: TenancyDatabase) -> None:
        await _seed_sessions(database)
        assert await _main(erase_actor.main, "alice", "--apply", "--all-tenants") == 0
        assert await _sessions(database) == {}
        records = erasure_ledger.read_records(ledger)
        assert sorted((r.actor, r.tenant) for r in records) == [
            ("alice", "acme"),
            ("alice", "globex"),
        ]

    await _in_two_tenants(monkeypatch, scenario)


async def test_a_tenant_that_is_not_registered_is_refused(
    tenancy_on: None,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A mistyped tenant is not a quiet run over nothing."""

    async def scenario(database: TenancyDatabase) -> None:
        await _seed_sessions(database)
        assert await _main(erase_actor.main, "alice", "--apply", "--tenant", "acmee") == 1
        assert "not a tenant registered" in capsys.readouterr().err
        assert len(await _sessions(database)) == 2

    await _in_two_tenants(monkeypatch, scenario)


def test_the_flags_change_nothing_while_tenancy_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-tenant deployment is not asked for a tenant."""
    monkeypatch.setattr(settings, "tenancy_enabled", False)
    forget_registered_tenants()
    from chemclaw.core.tenant_registry import choose_tenants

    assert asyncio.run(choose_tenants(None)) == ["default"]
    assert asyncio.run(choose_tenants("default")) == ["default"]
    with pytest.raises(Exception, match="tenancy is off"):
        asyncio.run(choose_tenants("acme"))


# --- every other command that touches tenant data -------------------------------------------

#: (module, whether `main` takes `argv`, arguments that pass parsing without naming a tenant).
#: A command that reads `sys.argv` is driven through it; a refusal is the first thing either does.
_COMMANDS = [
    ("chemclaw.retrieval.vector_index", True, []),
    ("chemclaw.cli.rekey_campaigns", True, []),
    ("chemclaw.cli.rekey_compounds", True, []),
    ("chemclaw.cli.backfill_publications", True, []),
    ("chemclaw.cli.sync_share", True, ["sharedrive"]),
    ("chemclaw.cli.explain", True, ["some-session"]),
    ("chemclaw.cli.synthesize", False, ["campaign"]),
    ("chemclaw.cli.distill", False, []),
    ("chemclaw.cli.propose_profile", False, []),
    ("chemclaw.cli.trajectory_census", False, []),
]


def _invoke(module: str, uses_argv: bool, argv: list[str]) -> int:
    """A command's real `main` with `argv`, its exit status however it reports it."""
    entry = importlib.import_module(module).main
    try:
        code = entry(argv) if uses_argv else entry()
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int | None) else 1
    return int(code or 0)


@pytest.mark.parametrize(("module", "uses_argv", "argv"), _COMMANDS, ids=[c[0] for c in _COMMANDS])
async def test_a_command_that_names_no_tenant_refuses_loudly(
    module: str,
    uses_argv: bool,
    argv: list[str],
    tenancy_on: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With tenancy on and no tenant named: a non-zero status that says how to name one."""
    monkeypatch.setattr(sys, "argv", [module, *argv])

    async def scenario(_database: TenancyDatabase) -> None:
        code = await asyncio.to_thread(_invoke, module, uses_argv, argv)
        assert code != 0, f"{module} exited 0 over nothing"
        assert "--tenant" in capsys.readouterr().err

    await _in_two_tenants(monkeypatch, scenario)


async def test_the_backfill_of_documents_refuses_too(
    tenancy_on: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """It takes a directory first, so it is driven apart from the table above."""

    async def scenario(_database: TenancyDatabase) -> None:
        code = await asyncio.to_thread(
            _invoke, "chemclaw.cli.backfill_corpus", True, [str(tmp_path)]
        )
        assert code != 0
        assert "--tenant" in capsys.readouterr().err

    await _in_two_tenants(monkeypatch, scenario)


async def test_a_command_acts_for_the_tenant_it_is_told(
    tenancy_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named, a campaign re-key runs for that tenant (a refusal above is the only other outcome)."""

    async def scenario(_database: TenancyDatabase) -> None:
        argv = ["--tenant", "acme"]
        assert await asyncio.to_thread(_invoke, "chemclaw.cli.rekey_campaigns", True, argv) == 0
        argv = ["--all-tenants"]
        assert await asyncio.to_thread(_invoke, "chemclaw.cli.rekey_campaigns", True, argv) == 0

    await _in_two_tenants(monkeypatch, scenario)
