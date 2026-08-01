"""Record a process metric from code that may not be running in the front door (REV-19).

`api/metrics.py` holds a process-wide registry, and a scrape targets a process. But most of
the code that has something worth counting does not know which process it is in: a durable job is
launched from the front door *and* from a Temporal worker, and a note reaches the PR-gate from
both. `agents` and `connectors` must not hard-depend on `service` — the workers import them and
never build the front door.

So the bridge is: import the registry lazily, apply the update, and tolerate a failure.

**This docstring used to say a failure means "no scrape target in this process", and that reading
was wrong in a way that cost the deployment its worker observability.** `api/metrics.py` is
stdlib-only and `chemclaw/api/__init__.py` is a docstring, so the import succeeds in *every*
process: a metric recorded in the background worker or a connector has always landed in a real,
live registry. What those processes lacked was a reader, and the chart's ServiceMonitor was written
on the strength of the sentence above — it scraped the front door alone, so everything the workers
counted was recorded and collected by nobody. `chemclaw.core.worker_http` is the reader, and this
swallow is what it always actually was: a guarantee that a metrics update cannot break the caller's
path, not a statement about where metrics exist.

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
