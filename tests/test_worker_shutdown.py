"""A worker is asked to stop on `SIGTERM` rather than killed.

Python installs no `SIGTERM` handler, so without `durable/serve.py` every drain, rollout and
eviction would kill the worker mid-activity: the activity is paid for twice, its retry waits out
start-to-close, and the pool never closes. Driven with a stand-in worker, because the behaviour is
in the runtime and a real worker needs a broker.
"""

import asyncio
import os
import signal
from typing import Any, cast

import pytest
from temporalio.worker import Worker

from chemclaw.durable.serve import serve_worker


class _FakeCount:
    """What `Client.count_workflows` returns: an object with a `count`."""

    count = 0


class _FakeClient:
    """The one client method `serve_worker` reaches for, through the in-flight gauge's refresh.

    Without it every test here would exercise the refresh's degrade path instead of its normal one.
    """

    async def count_workflows(self, query: str) -> _FakeCount:
        """Answer nothing is running, without a broker."""
        return _FakeCount()


class _FakeWorker:
    """A worker that runs until told to shut down, recording what it was asked to do.

    Shaped after the real contract: `run()` returns only once `shutdown()` is called, and
    `shutdown()` waits for the drain.
    """

    def __init__(
        self, *, fail: BaseException | None = None, fail_while_draining: bool = False
    ) -> None:
        """Start not-running; `fail` is raised by `run()` — at once, or on the way out of the drain.

        Two timings, two bugs: a worker that cannot start, and one that breaks while finishing its
        last activity, which a runtime that stops awaiting after `shutdown()` would lose.
        """
        self.is_running = False
        self.client = _FakeClient()
        self.shutdown_calls = 0
        self.drained = False
        self._stop = asyncio.Event()
        self._fail = fail
        self._fail_while_draining = fail_while_draining

    async def run(self) -> None:
        """Poll until `shutdown` releases us (or fail, at whichever of the two points is asked)."""
        if self._fail is not None and not self._fail_while_draining:
            raise self._fail
        self.is_running = True
        await self._stop.wait()
        self.is_running = False
        if self._fail is not None:
            raise self._fail

    async def shutdown(self) -> None:
        """Stop polling and let in-flight work finish — the half a SIGKILL skipped."""
        self.shutdown_calls += 1
        await asyncio.sleep(0)  # a drain is not instantaneous; make the await real
        self.drained = True
        self._stop.set()


@pytest.fixture(autouse=True)
def _no_bound_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the shutdown, so skip the probe surface rather than bind a port."""
    monkeypatch.setattr("chemclaw.core.config.settings.worker_metrics_port", 0)


def _serve(worker: Any, *, then: Any) -> Any:
    """Run `serve_worker` against `worker` while `then()` drives the process."""

    async def _exercise() -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(serve_worker(worker, component="test-worker"))
            group.create_task(then())

    return asyncio.run(_exercise())


async def _sigterm_once_polling(worker: _FakeWorker) -> None:
    """Wait for the worker to actually be polling, then ask the process to stop."""
    while not worker.is_running:
        await asyncio.sleep(0.01)
    os.kill(os.getpid(), signal.SIGTERM)


def test_a_stop_signal_drains_instead_of_killing() -> None:
    """The finding: SIGTERM used to end the process, not the polling.

    A drained worker has stopped taking new tasks and let its in-flight ones finish. A killed one
    has done neither, and the difference is invisible from outside — both leave a pod that is gone.
    """
    worker = _FakeWorker()
    _serve(worker, then=lambda: _sigterm_once_polling(worker))

    assert worker.shutdown_calls == 1, "SIGTERM did not reach the worker as a shutdown"
    assert worker.drained, "the runtime stopped waiting before the drain completed"


def test_sigint_drains_the_same_way() -> None:
    """A developer's Ctrl-C takes the path the cluster takes, rather than a different one.

    Two shutdown paths means the one that runs in production is the one nobody exercises.
    """
    worker = _FakeWorker()

    async def _interrupt() -> None:
        while not worker.is_running:
            await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    _serve(worker, then=_interrupt)
    assert worker.drained


def test_a_fatal_worker_error_is_raised_not_drained() -> None:
    """The one case where the process *should* end loudly.

    Swallowing a fatal error in the drain would make the pod exit 0, read as a completed job rather
    than a crash to restart and alert on.
    """
    worker = _FakeWorker(fail=RuntimeError("worker fatal error"))

    async def _nothing() -> None:
        return None

    with pytest.raises(ExceptionGroup) as raised:
        _serve(worker, then=_nothing)
    # The group is the test harness's `TaskGroup`, not the runtime's — what matters is that the
    # worker's own error is what came out of it, unchanged.
    assert [str(error) for error in raised.value.exceptions] == ["worker fatal error"]
    assert worker.shutdown_calls == 0, "a fatal error was routed through the drain"


def test_a_failure_during_the_drain_is_not_swallowed() -> None:
    """What the final `await` on the run task is actually for.

    `shutdown()` already waits for the drain, but an error `run()` raises while finishing its last
    activity is only surfaced by awaiting the run task; without it the pod exits 0.
    """
    worker = _FakeWorker(fail=RuntimeError("broke while draining"), fail_while_draining=True)

    with pytest.raises(ExceptionGroup) as raised:
        _serve(worker, then=lambda: _sigterm_once_polling(worker))
    assert [str(error) for error in raised.value.exceptions] == ["broke while draining"]
    assert worker.shutdown_calls == 1, "the drain was never attempted"


def test_the_handlers_do_not_outlive_the_worker() -> None:
    """`serve_worker` puts the process's signal disposition back the way it found it.

    A leftover handler would silently claim SIGTERM from whatever ran next. Asserted via
    `signal.getsignal` inside the loop, because `asyncio.run` closing the loop removes handlers
    anyway and an assertion afterwards would pass with no cleanup.
    """
    worker = _FakeWorker(fail=RuntimeError("stop"))

    async def _exercise() -> tuple[Any, Any]:
        before = signal.getsignal(signal.SIGTERM)
        with pytest.raises(RuntimeError):
            # Cast rather than a Protocol on `serve_worker`: it uses three members of `Worker`, and
            # widening a production signature to admit a test double buys nothing the cast does not.
            await serve_worker(cast(Worker, worker), component="test-worker")
        return before, signal.getsignal(signal.SIGTERM)

    before, after = asyncio.run(_exercise())
    assert after is before, "SIGTERM is still routed to a worker that has finished"


def test_the_pool_and_the_probe_surface_close_with_it() -> None:
    """The drain is what makes the surrounding `async with` unwind at all.

    On a hard kill `db.pooling()`'s exit never runs and connections are dropped rather than closed.
    """
    closed: list[str] = []

    class _Tracked:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            closed.append("pool")

    worker = _FakeWorker()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("chemclaw.durable.serve.db.pooling", lambda: _Tracked())
        _serve(worker, then=lambda: _sigterm_once_polling(worker))

    assert closed == ["pool"], "the pool was not closed on the way out"
