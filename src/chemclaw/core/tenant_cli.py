"""The tenant flags an operator command takes, and the refusal when tenancy is on and none is named.

Row-level security applies to the owner and to an operator's connection alike, so a command that
names no tenant reads and rewrites nothing and still exits 0. Every command that touches tenant data
takes `--tenant <id>` (and, where its work is the same for everyone, `--all-tenants`), runs its work
once per chosen tenant with that tenant bound, and exits non-zero naming the remedy when it was told
neither. With tenancy off there is one tenant and the flags change nothing.
"""

import argparse
import sys
from collections.abc import Awaitable, Callable
from typing import TypeVar

from chemclaw.core.tenancy import TenantError, bound_tenant
from chemclaw.core.tenant_registry import choose_tenants

_T = TypeVar("_T")

#: The exit status of a command that refused to run for want of a tenant.
EXIT_NO_TENANT = 2


def add_tenant_flags(parser: argparse.ArgumentParser, *, all_tenants: bool) -> None:
    """Add `--tenant`, and `--all-tenants` where the command's work is the same for every tenant."""
    parser.add_argument(
        "--tenant",
        default=None,
        help="The tenant to act for. Required when tenancy is enabled and the process names none "
        "(CHEMCLAW_TENANCY_PROCESS_TENANT).",
    )
    if all_tenants:
        parser.add_argument(
            "--all-tenants",
            action="store_true",
            help="Act for every tenant registered for this release, one after another.",
        )


async def for_chosen_tenants(
    args: argparse.Namespace, work: Callable[[str], Awaitable[_T]]
) -> dict[str, _T]:
    """`work(tenant)` for each tenant the flags choose, with that tenant bound, in id order.

    Raises:
        TenantError: nothing was chosen under tenancy, or the choice is not a tenant here.
    """
    tenants = await choose_tenants(
        getattr(args, "tenant", None), all_tenants=bool(getattr(args, "all_tenants", False))
    )
    results: dict[str, _T] = {}
    for tenant in tenants:
        with bound_tenant(tenant):
            results[tenant] = await work(tenant)
    return results


def refusal(exc: TenantError, *, status: int = EXIT_NO_TENANT) -> int:
    """Print why the command did not run, and the exit status that says so."""
    print(f"refused: {exc}", file=sys.stderr)
    return status
