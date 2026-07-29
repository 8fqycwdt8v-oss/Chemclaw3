"""The `qm` connector's own Temporal worker — the durable half of this bundle.

Run it with `python -m chemclaw.connectors.qm.worker`. It polls `connector-qm` and serves whatever
importing
this bundle's modules registered. The HPC launcher credential and the 24-hour poll live in this
process and nowhere else, because this import happens here and core's workers never make it.
"""

from chemclaw.connectors.qm import (
    activities as _activities,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.qm import workflows as _workflows  # noqa: F401 — registration side effect
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("qm")
