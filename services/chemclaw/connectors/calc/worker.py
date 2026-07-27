"""The `calc` connector's own Temporal worker — the durable half of this bundle.

Run it with `python -m connectors.calc.worker`. It polls one queue (`connector-calc`, the one its
manifest declares) and serves one workflow. Core's worker serves neither, and core imports neither:
`ConnectorJobWorkflow` reaches this workflow by type name across the queue, which is what keeps
`tblite`, RDKit, SciPy and the `xtb`/`crest` binaries loaded in this process and the MCP server's,
and nowhere else.

The same fifteen lines as `connectors/bo/worker.py`, and still deliberately not shared. Two callers
is where the Rule of Three says to look again, not to act: what differs between them is the queue
name and the two registration lists — the whole body — so a "connector worker framework" would
abstract the imports and leave the substance. The third one decides it.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.logging import configure_logging, configure_telemetry
from chemclaw.temporal_client import connect
from connectors.calc.activities import run_xtb_calculation
from connectors.calc.workflows import CalcJobWorkflow

logger = logging.getLogger(__name__)

# The queue this bundle's manifest names. A module constant rather than config: it is part of the
# connector's contract with core (`connector.yaml`), not a per-deployment knob — changing it means
# changing the manifest in the same commit.
TASK_QUEUE = "connector-calc"

CALC_WORKFLOWS: list[type] = [CalcJobWorkflow]
CALC_ACTIVITIES: Sequence[Callable[..., Any]] = [run_xtb_calculation]


async def main() -> None:
    """Connect and poll the connector's own queue for expensive calculations."""
    configure_logging()
    configure_telemetry()
    client = await connect()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=CALC_WORKFLOWS,
        activities=CALC_ACTIVITIES,
    )
    logger.info("calc connector worker connected: queue=%s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
