"""Record a process metric from code that may not be running in the front door (REV-19).

`api/metrics.py` holds a process-wide registry, and a scrape targets a process. But most of
the code that has something worth counting does not know which process it is in: a durable job is
launched from the front door *and* from a Temporal worker, and a note reaches the PR-gate from
both. `agents` and `connectors` must not hard-depend on `service` — the workers import them and
never build the front door.

So the bridge is: import the registry lazily, apply the update, and tolerate its absence. A missing
registry means "no scrape target in this process", which is the truth rather than an error.

This began as a private helper in `agent/audit.py` with two callers (the audit-sink failure
counter, the tool-latency histogram). It moved here at the fourth, rather than being imported
across modules by its underscore name. The swallow-all is written once on purpose: a second copy of
a bare `except Exception: pass` is exactly where a real error goes to hide.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the front door's registry; nothing here may import `service` at runtime
    from chemclaw.api.metrics import Metrics


def record_metric(update: "Callable[[Metrics], None]") -> None:
    """Apply `update` to the process metrics registry, tolerating one that cannot be imported."""
    try:
        from chemclaw.api.metrics import METRICS

        update(METRICS)
    except Exception:  # pragma: no cover - defensive; metrics must never break the caller's path
        pass
