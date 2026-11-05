"""A uvicorn server on a background thread, started and stopped around one test.

`TestClient` and httpx's ASGI transport buffer a response whole and run each request on its own
loop, so a test about a dropped stream, or a second request while one is open, needs a real
listener. This is the one place a test starts one.

- The port is bound when the server is built and handed to uvicorn as an open socket, so nothing
  can take it between "find a free port" and "listen on it".
- Start waits on the server's own readiness event, not a poll, under a bound that scales with
  `PYTEST_TIMEOUT_SCALE`.
- A server that dies while starting raises that cause. uvicorn answers a lifespan that raised, or
  a port that was taken, with `sys.exit(3)`, which a thread drops without a word.
"""

import logging
import socket
import threading
import time
from types import TracebackType
from typing import Any, Self

import uvicorn

from tests.conftest import timeout_scale

#: How long a server has to finish starting, app lifespan included, before the test gives up.
#: A start is tens of milliseconds on an idle machine; this bounds a starved one, scaled by
#: `PYTEST_TIMEOUT_SCALE` like every other wall-clock cap in the suite.
STARTUP_SECONDS = 60.0

#: How long a stopping server has to unwind before its thread is abandoned.
SHUTDOWN_SECONDS = 10.0


class _Signalling(uvicorn.Server):
    """A `uvicorn.Server` that sets `settled` when its startup returns or its thread ends."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.settled = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Run uvicorn's startup, then say it is over; a startup that exits never reaches this."""
        await super().startup(sockets=sockets)
        self.settled.set()


class _Errors(logging.Handler):
    """Keeps what uvicorn logged at ERROR while starting, because that is where it says why."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the message and its traceback."""
        self.lines.append(self.format(record))


#: The loggers `uvicorn.Config` reconfigures through `logging.config.dictConfig` when it is built:
#: own handlers, `propagate=False`. Left that way they stop reaching the root logger (and pytest's
#: `caplog`) for the rest of the process, so a server restores them when its test ends.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _logger_state() -> list[tuple[logging.Logger, list[logging.Handler], bool, int, bool]]:
    """Each uvicorn logger with its handlers, `propagate`, level and `disabled`, as they are now."""
    loggers = [logging.getLogger(name) for name in _UVICORN_LOGGERS]
    return [(lg, list(lg.handlers), lg.propagate, lg.level, lg.disabled) for lg in loggers]


def _restore(state: list[tuple[logging.Logger, list[logging.Handler], bool, int, bool]]) -> None:
    """Put each logger back as `_logger_state` found it."""
    for lg, handlers, propagate, level, disabled in state:
        lg.handlers[:] = handlers
        lg.propagate, lg.level, lg.disabled = propagate, level, disabled


class ServerThread:
    """A uvicorn server on a background thread; use as a context manager around one test."""

    def __init__(self, app: Any, port: int = 0, **config: Any) -> None:
        """Serve `app` on loopback `port` (0: OS picks); `config` goes to `uvicorn.Config`."""
        # Before `uvicorn.Config`, which is what rewrites them.
        self._loggers = _logger_state()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", port))
        self.port: int = self._socket.getsockname()[1]
        self._server = _Signalling(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", **config)
        )
        self._died: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        """Serve until told to exit; keep whatever ended the thread, `SystemExit` included."""
        try:
            self._server.run(sockets=[self._socket])
        except BaseException as exc:  # kept for the starter to raise, not swallowed
            self._died = exc
        finally:
            self._server.settled.set()

    def __enter__(self) -> Self:
        """Start the thread and block until the server is accepting, or raise why it is not."""
        errors = _Errors()
        uvicorn_logger = logging.getLogger("uvicorn.error")
        uvicorn_logger.addHandler(errors)
        began = time.monotonic()
        deadline = STARTUP_SECONDS * timeout_scale()
        try:
            self._thread.start()
            settled = self._server.settled.wait(deadline)
        finally:
            uvicorn_logger.removeHandler(errors)
        if self._server.started:
            return self
        self._server.should_exit = True
        self._socket.close()
        _restore(self._loggers)
        reason = "; ".join(errors.lines) or "nothing logged"
        if not settled:
            raise TimeoutError(
                f"the server on port {self.port} had not finished starting after {deadline:g}s; "
                f"uvicorn logged: {reason}"
            )
        raise RuntimeError(
            f"the server on port {self.port} stopped {time.monotonic() - began:.1f}s after "
            f"starting without ever serving: {self._died!r}; uvicorn logged: {reason}"
        ) from self._died

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Ask uvicorn to exit and wait for the thread, so no server outlives its test."""
        self._server.should_exit = True
        self._thread.join(timeout=SHUTDOWN_SECONDS)
        self._socket.close()
        _restore(self._loggers)
