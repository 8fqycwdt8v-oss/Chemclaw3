"""The `calc` connector's own Temporal worker: the durable half of this bundle.

Run it with `python -m chemclaw.connectors.calc.worker`. It polls `connector-calc` and serves
whatever importing this bundle's modules registered; core's workers never import them.
"""

from chemclaw.connectors.calc import (
    activities as _activities,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.calc import (
    workflows as _workflows,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.worker import main

if __name__ == "__main__":
    main("calc")
