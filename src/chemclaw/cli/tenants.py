"""Register and list the tenants a deployment serves.

A tenant exists because an operator ran `add`, never because a token named it: a `tenant:<id>` role
for an id nobody registered is refused at the front door. Run under the owning principal
(`postgres_migration_dsn`), like the migrations: the runtime role can read the registry and cannot
write it, so a request cannot create a tenant.

`add` is idempotent. There is no `remove`: a tenant's rows would be orphaned, and the schema only
goes forward.

Run: `python -m chemclaw.cli.tenants add <id>` or `python -m chemclaw.cli.tenants list`
"""

import argparse
import asyncio
import logging
import sys

from chemclaw.core.db import connect
from chemclaw.core.logging import configure_logging
from chemclaw.core.migrate import migration_dsn
from chemclaw.core.tenancy import require_tenant_id

logger = logging.getLogger(__name__)

_INSERT = "INSERT INTO tenants (id) VALUES (%s) ON CONFLICT (id) DO NOTHING"
_LIST = "SELECT id, created_at FROM tenants ORDER BY id"


async def add_tenant(tenant: str) -> bool:
    """Register `tenant`; whether it was new.

    Raises:
        TenantError: `tenant` is not a tenant id.
    """
    async with await connect(migration_dsn()) as conn:
        cursor = await conn.execute(_INSERT, (require_tenant_id(tenant),))
        await conn.commit()
        return cursor.rowcount == 1


async def list_tenants() -> list[tuple[str, str]]:
    """Every registered tenant and when it was registered, in id order."""
    async with await connect(migration_dsn()) as conn:
        cursor = await conn.execute(_LIST)
        return [(str(row[0]), row[1].isoformat()) for row in await cursor.fetchall()]


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one subcommand."""
    parser = argparse.ArgumentParser(prog="python -m chemclaw.cli.tenants", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register a tenant (idempotent).")
    add.add_argument(
        "tenant", help="Lowercase letters, digits, '_' and '-'; at most 63 characters."
    )
    commands.add_parser("list", help="Print every registered tenant.")
    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "add":
        created = asyncio.run(add_tenant(args.tenant))
        print(f"{'registered' if created else 'already registered'}: {args.tenant}")
        return 0
    for tenant, registered in asyncio.run(list_tenants()):
        print(f"{tenant}\t{registered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
