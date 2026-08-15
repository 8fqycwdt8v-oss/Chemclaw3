"""Evaluation & metric layer (plan Phase 2b).

Importing the package registers the seed metrics (via `chemclaw.evals.metrics`), so callers can
resolve them by name straight away. Public surface: the metric interface + registry
(`metric`), the eval harness (`harness`), and the tool-utility A/B (`ab`).
"""

from chemclaw.evals import (
    autonomy as _autonomy,
)
from chemclaw.evals import (
    metrics as _metrics,
)
from chemclaw.evals import (
    retrieval as _retrieval,
)

__all__ = ["_autonomy", "_metrics", "_retrieval"]
