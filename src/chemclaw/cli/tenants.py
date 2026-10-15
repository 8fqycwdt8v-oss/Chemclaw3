"""Register and list the tenants a deployment serves.

A tenant exists because an operator ran `add`, never because a token named it: a `tenant:<id>` role
for an id nobody registered is refused at the front door. Run under the owning principal
(`postgres_migration_dsn`), like the migrations: the runtime role can read the registry and cannot
write it, so a request cannot create a tenant.

`add` is idempotent. There is no `remove`: a tenant's rows would be orphaned, and the schema only
goes forward. Where two releases share one database, `--deployment <id>` gives a tenant to the
release whose `CHEMCLAW_DEPLOYMENT_ID` is that, and no other release serves it; `add` leaves a
registered tenant's owner alone and `assign` changes it.

Run: `python -m chemclaw.cli.tenants add <id> [--deployment <release>]`, `... assign <id>
<release>` or `... list`
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

_INSERT = "INSERT INTO tenants (id, deployment_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING"
_ASSIGN = "UPDATE tenants SET deployment_id = %s WHERE id = %s"
_LIST = "SELECT id, created_at, deployment_id FROM tenants ORDER BY id"


async def add_tenant(tenant: str, deployment: str | None = None) -> bool:
    """Register `tenant`, for release `deployment` alone when given; whether it was new.

    Raises:
        TenantError: `tenant` is not a tenant id.
    """
    async with await connect(migration_dsn()) as conn:
        cursor = await conn.execute(_INSERT, (require_tenant_id(tenant), deployment or None))
        await conn.commit()
        return cursor.rowcount == 1


async def assign_tenant(tenant: str, deployment: str) -> bool:
    """Give `tenant` to release `deployment`, which then alone serves it; whether it exists.

    Raises:
        TenantError: `tenant` is not a tenant id.
    """
    async with await connect(migration_dsn()) as conn:
        cursor = await conn.execute(_ASSIGN, (deployment, require_tenant_id(tenant)))
        await conn.commit()
        return cursor.rowcount == 1


async def list_tenants() -> list[tuple[str, str, str]]:
    """Every registered tenant, when it was registered and its release (`-`: any), in id order."""
    async with await connect(migration_dsn()) as conn:
        cursor = await conn.execute(_LIST)
        return [(str(r[0]), r[1].isoformat(), str(r[2] or "-")) for r in await cursor.fetchall()]


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one subcommand."""
    parser = argparse.ArgumentParser(prog="python -m chemclaw.cli.tenants", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Register a tenant (idempotent).")
    add.add_argument(
        "tenant", help="Lowercase letters, digits, '_' and '-'; at most 63 characters."
    )
    add.add_argument(
        "--deployment",
        default=None,
        help="Serve this tenant from the release with this CHEMCLAW_DEPLOYMENT_ID only.",
    )
    assign = commands.add_parser("assign", help="Give a registered tenant to one release.")
    assign.add_argument("tenant", help="A registered tenant id.")
    assign.add_argument("deployment", help="The release's CHEMCLAW_DEPLOYMENT_ID.")
    commands.add_parser("list", help="Print every registered tenant.")
    args = parser.parse_args(argv)
    configure_logging()
    if args.command == "add":
        created = asyncio.run(add_tenant(args.tenant, args.deployment))
        print(f"{'registered' if created else 'already registered'}: {args.tenant}")
        return 0
    if args.command == "assign":
        found = asyncio.run(assign_tenant(args.tenant, args.deployment))
        print(f"{'assigned' if found else 'not registered'}: {args.tenant}")
        return 0 if found else 1
    for tenant, registered, release in asyncio.run(list_tenants()):
        print(f"{tenant}\t{registered}\t{release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
