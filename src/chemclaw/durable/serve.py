"""Run a Temporal worker so that a pod termination finishes its work instead of losing it.

`asyncio.run(main())` around `worker.run()` looks complete and has no shutdown in it at all. Python
installs no `SIGTERM` handler, so the default disposition applies: the process dies **immediately**,
mid-activity, with no unwinding — and that is what every worker in this system did on a node drain,
a rolling update, an HPA scale-down or an eviction. Temporal makes the *work* survivable (the
activity is retried on another worker after its start-to-close timeout expires) but survivable is
not free:

- A long activity is re-run from the beginning, so an ELN sync or a report is paid for twice.
- The retry does not begin until the timeout elapses, which for `calc` is a CREST search's whole
  budget. A deploy therefore stalls a job by up to that timeout for no reason other than how it
  was killed.
- The pod's own cleanup never runs: `db.pooling()`'s connections are dropped rather than closed, and
  a git checkout the PR-gate submitter was mid-way through is abandoned in place.

`Worker.shutdown()` is the supported alternative — stop polling for new tasks, let in-flight ones
finish, then cancel what remains after `graceful_shutdown_timeout`. It just needs something to call
it, and a signal handler is that something.

**One function rather than two shared helpers.** A worker process needs three things wired: the
Postgres pool, the probe/scrape surface (`core/worker_http.py`), and this shutdown. They are wired
identically in both entrypoints, and a third worker wiring two of the three would be a pod that
looks healthy while doing the wrong thing on termination — the exact failure mode
`D-2026-08-01-every-process-carries-its-own-witness` had just finished closing for probes. So the
tail of every worker's `main()` is a single call.
"""

import asyncio
import logging
import signal

from temporalio.worker import Worker

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.executor import install_default_executor
from chemclaw.core.worker_http import worker_http

logger = logging.getLogger(__name__)

# The signals a container runtime uses to ask for a shutdown. SIGTERM is what the kubelet sends
# before the grace period; SIGINT is Ctrl-C, so a developer's local worker drains the same way the
# cluster's does rather than through a different code path that has never been exercised.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


async def serve_worker(worker: Worker, *, component: str) -> None:
    """Poll until asked to stop, then drain — with the pool open and the probes answering.

    Args:
        worker: An already-built Temporal worker. Built by the caller, not here, because what a
            worker *serves* is the one thing that genuinely differs between them — and because
            `graceful_shutdown_timeout` belongs at the constructor where a reader looks for it.
        component: What this process is (`background-worker`, `connector-worker-calc`), for the
            health
            payloads and the log line.

    A worker fatal error propagates rather than being swallowed by the drain: it is the one case
    where the process *should* end loudly, and Temporal's own `run()` docstring says `shutdown()`
    need not be invoked for it.
    """
    loop = asyncio.get_running_loop()
    # Before the worker polls for its first task. Several activities offload blocking work — the
    # note-corpus read, the RRHO arithmetic, the fingerprint scans — and they share one pool with
    # whatever else this process threads. The loop's stock default is `min(32, cpu_count + 4)`,
    # which on a 4-CPU pod is 8: exactly `worker_max_concurrent_activities`, so a full slate of
    # activities could occupy every thread and anything else needing one would queue behind a
    # corpus parse. See `core/executor.py`.
    install_default_executor(
        component=component, reserved=settings.worker_max_concurrent_activities
    )
    stop = asyncio.Event()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    try:
        # Every activity here is a coroutine on this process's one event loop, so a per-call
        # Postgres handshake is loop time stolen from task polling and heartbeats. Pooled for the
        # worker's whole life and closed on shutdown — which is a promise only kept because the
        # signal handler above lets the `async with` actually unwind.
        async with db.pooling(), worker_http(component=component, ready=lambda: worker.is_running):
            running = asyncio.create_task(worker.run())
            waiting = asyncio.create_task(stop.wait())
            await asyncio.wait({running, waiting}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            if running.done():  # a fatal worker error, or a shutdown from somewhere else
                await running
                return
            logger.info("%s: draining (stop signal received)", component)
            await worker.shutdown()
            await running
            logger.info("%s: drained", component)
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
