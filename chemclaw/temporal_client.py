"""One place to open a Temporal client, configured consistently.

Both the worker (`workers/`) and the agent's job tools (`agents/`, Phase 1.5+)
need a client that points at the configured address/namespace and uses the
pydantic data converter so our models serialize losslessly. Extracted here so
that wiring is written once, not copied per caller (DRY).
"""

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from chemclaw.config import settings


async def connect() -> Client:
    """Connect to Temporal using the configured address, namespace, and converter."""
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
