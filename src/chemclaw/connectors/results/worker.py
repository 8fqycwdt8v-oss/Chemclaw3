"""The `results` connector's own Temporal worker.

Run it with `python -m chemclaw.connectors.results.worker`. It polls `connector-results` and serves
whatever importing this bundle's modules registered. A corpus walk over two never-pruned tables
belongs on its own queue rather than beside the many small background jobs.
"""

from chemclaw.connectors.results import (
    workflows as _workflows,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("results")
