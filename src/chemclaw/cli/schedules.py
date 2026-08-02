"""Entrypoint for `make schedules-apply`: apply the Temporal Schedules for the periodic jobs.

The Schedule definitions and the apply/prune logic are durable-layer library code — a Temporal
Schedule is Temporal's own durability primitive — and live in `chemclaw.durable.schedules`, which
`chemclaw.api.app` also imports at module scope for the `/schedules` health endpoint. This module
is only the CLI shim: `python -m chemclaw.cli.schedules` (what `make schedules-apply` runs)
connects to Temporal and applies the plan.
"""

import asyncio

from chemclaw.core.logging import configure_logging
from chemclaw.core.temporal_client import connect
from chemclaw.durable.schedules import apply_schedules


async def main() -> None:
    """Connect to Temporal and apply the periodic-job Schedules."""
    configure_logging()
    client = await connect()
    await apply_schedules(client)


if __name__ == "__main__":
    asyncio.run(main())
