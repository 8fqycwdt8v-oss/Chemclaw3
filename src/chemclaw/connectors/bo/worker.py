"""The `bo` connector's own Temporal worker — what makes it a connector-owned durable capability.

Run it with `python -m chemclaw.connectors.bo.worker`. It polls one queue (`connector-bo`, derived
from the
bundle name) and serves whatever importing this bundle's modules registered. Core's worker serves
neither and imports neither: `ConnectorJobWorkflow` reaches this workflow by type name across the
queue, which is the whole point of the seam — `bofire` and `botorch` are loaded in this process and
nowhere else, precisely *because* this import happens here and not there.
"""

from chemclaw.connectors.bo import (
    activities as _activities,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.bo import workflows as _workflows  # noqa: F401 — registration side effect
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("bo")
