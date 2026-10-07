"""The `bo` connector's own Temporal worker.

Run with `python -m chemclaw.connectors.bo.worker`. It polls `connector-bo` (derived from the bundle
name) and serves what this bundle's modules registered. Core's worker imports none of it, so
`bofire` and `botorch` load in this process only; `ConnectorJobWorkflow` reaches the workflow by
type name.
"""

from chemclaw.connectors.bo import (
    activities as _activities,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.bo import workflows as _workflows  # noqa: F401 — registration side effect
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("bo")
