"""Evaluation & metric layer (plan Phase 2b).

Importing the package registers the seed metrics (via `evals.metrics`), so callers can
resolve them by name straight away. Public surface: the metric interface + registry
(`metric`), the eval harness (`harness`), and the tool-utility A/B (`ab`).
"""

from evals import metrics as _metrics  # noqa: F401 — imported for its registration side effect

__all__ = ["_metrics"]
