"""The `results` connector's own Temporal worker.

Run it with `python -m chemclaw.connectors.results.worker`. It polls `connector-results`, keeping a
corpus walk over two never-pruned tables off the light background queue.
"""

from chemclaw.connectors.results import (
    workflows as _workflows,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("results")
