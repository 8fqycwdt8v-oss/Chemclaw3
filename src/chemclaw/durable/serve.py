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

from temporalio.worker import Interceptor, Worker

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.executor import install_default_executor
from chemclaw.core.logging import log_event
from chemclaw.core.worker_http import worker_http
from chemclaw.durable.interceptor import ChemclawWorkerInterceptor, activities_in_flight, draining
from chemclaw.durable.job_metrics import bind_job_gauges, poll_open_jobs

logger = logging.getLogger(__name__)

# The signals a container runtime uses to ask for a shutdown. SIGTERM is what the kubelet sends
# before the grace period; SIGINT is Ctrl-C, so a developer's local worker drains the same way the
# cluster's does rather than through a different code path that has never been exercised.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def worker_interceptors() -> list[Interceptor]:
    """The interceptor chain every `Worker` in this system adds to the client's own.

    One function rather than a literal at each constructor, for the reason this module exists at
    all: two entrypoints wiring different subsets of the cross-cutting concerns is a pod that looks
    healthy while doing less than the other one. A third worker gets the whole chain by calling
    this, or gets none of it visibly.

    **The tracing interceptor is deliberately not here, and used to be.** A `Worker` does not use
    the list it is given as the chain; `temporalio.worker._worker` prepends the interceptors the
    *client* already carries (`interceptors_from_client + list(config["interceptors"])`), and
    `core/temporal_client.py::connect_options` puts a `TracingInterceptor` on every client when
    `otel_enabled`. Measured on 2026-08-28 against a live broker, the chain a worker actually ran
    was `['TracingInterceptor', 'ChemclawWorkerInterceptor', 'TracingInterceptor']` — every
    activity and workflow traced twice, from one interceptor added in two places.

    That measurement also corrects what this docstring used to claim. It said "the observability
    interceptor is outermost so its log line and its failure counter bracket everything"; the SDK
    wraps in reverse list order, so the *client's* tracing interceptor is outermost and ours runs
    inside it, whatever this function returns. Which is the right way round: a span that does not
    enclose the log line and the failure counter it explains is a span that ends before the thing
    it is measuring does.
    """
    return [ChemclawWorkerInterceptor()]


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
    # Before the probe surface opens, so the first scrape already has a reading rather than a
    # missing series. Here for the same reason the pool and the probes are: this is the one tail
    # every worker's `main()` runs through, so no entrypoint can wire the drain and forget the
    # gauge.
    bind_job_gauges()
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
            # The gauge's reading, refreshed against the broker rather than kept by a workflow
            # body — see `durable/job_metrics.py` for the three live measurements that retired the
            # process-local set this replaced. Cancelled however this function leaves, so it never
            # outlives the client it queries; kept alive *through* the drain, so `/metrics` does
            # not freeze at the moment an operator is watching a shutdown.
            polling = asyncio.create_task(poll_open_jobs(worker.client, stop))
            try:
                await asyncio.wait({running, waiting}, return_when=asyncio.FIRST_COMPLETED)
                waiting.cancel()
                if running.done():  # a fatal worker error, or a shutdown from somewhere else
                    await running
                    return
                # **The count, not just the fact.** This module's own docstring names the cost of
                # a hard kill — a long activity re-run from the beginning, paid for twice — and the
                # two log lines said only "draining" and "drained", so nothing anywhere reported
                # what the drain was actually carrying. Work is not *lost* (Temporal redelivers),
                # which is exactly why it needs a number: a silent second payment leaves no other
                # trace.
                #
                # Activities and not durable jobs, because an activity is what a drain can actually
                # lose: a cancelled one is redelivered and paid for twice, while an evicted parent
                # workflow is picked up by another worker with no work repeated. This line used to
                # report both, taking the second figure from a process-local set that read the
                # *wrong number* under exactly this event (`durable/job_metrics.py`).
                log_event(
                    logger,
                    "worker.draining",
                    "%s: draining with %d activity/activities in flight",
                    component,
                    activities_in_flight(),
                    component=component,
                    activities_in_flight=activities_in_flight(),
                    budget_seconds=settings.worker_graceful_shutdown_seconds,
                )
                # Whatever `graceful_shutdown_timeout` does not cover is cancelled by `shutdown()`,
                # and each cancellation is counted where it is observed: inside the interceptor,
                # the only frame that sees one. See `durable/interceptor.py::draining`.
                with draining():
                    await worker.shutdown()
                    await running
                log_event(
                    logger,
                    "worker.drained",
                    "%s: drained",
                    component,
                    component=component,
                )
            finally:
                polling.cancel()
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
