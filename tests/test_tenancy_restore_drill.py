"""The restore drill on a database migrated with tenancy on.

`run_module` hands its subprocesses this process's environment, so a drill run where
`CHEMCLAW_TENANCY_ENABLED=true` migrates a database whose policies are forced, and every raw
connection the drill then opens binds no tenant: its inserts are refused and its counts are zero.
The drill acts for one tenant (`DRILL_TENANT`), bound on each connection it opens.
"""

import asyncio

from chemclaw.core import restore_check as drill
from chemclaw.core.db import connect
from tests.tenancy_db import TenancyDatabase, enable_tenancy, tenancy_database


async def _strip_update(database: TenancyDatabase) -> None:
    """Make the test role append-only on the audit trail, as the shipped grants make `APP_ROLE`."""
    async with await connect(database.owner_dsn) as conn:
        await conn.execute(f'REVOKE UPDATE, DELETE ON audit_events FROM "{database.role}"')
        await conn.commit()


async def test_a_wave_of_drill_data_loads_and_counts_under_forced_policies() -> None:
    """The drill's writes and its snapshot work as a role the policies bind."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        await asyncio.to_thread(drill.load_wave, database.app_dsn, "wave", 2, 7)
        counts = await asyncio.to_thread(drill.snapshot, database.app_dsn)
        assert counts.counts["session_owners"] == 2
        assert counts.counts["audit_events"] >= 2


async def test_the_runtime_role_check_appends_under_forced_policies() -> None:
    """Appending to the audit trail as the runtime role is not refused by the policy."""
    async with tenancy_database() as database:
        await enable_tenancy(database)
        await _strip_update(database)
        check = await asyncio.to_thread(drill.app_role_check, database.app_dsn)
        assert check.status == "pass", check.detail
