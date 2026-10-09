"""Launching `tests/replica_process.py`: real front-door processes on one database.

A test that claims something about separate replicas (a limit that holds across them, a write that
survives a kill) needs separate processes; threads in one process share the in-memory state the
claim is about. The processes inherit the test's redirected DSN, so they share the test schema.
"""

import contextlib
import ctypes
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx

from chemclaw.core.config import settings

#: The marker the lane puts in the reason of a skip, so `tests/conftest.py` can count it.
LANE_SKIP = "Multi-replica lane unavailable"

#: How long a started process has to answer its readiness probe before the lane gives up on it.
READY_SECONDS = 90.0

#: `PR_SET_PDEATHSIG` from `<linux/prctl.h>`.
_PR_SET_PDEATHSIG = 1


def die_with_parent() -> None:
    """Run in the child between fork and exec: SIGKILL it when the process that started it dies.

    A test runner that is itself SIGKILLed runs no `finally`, and a replica or worker left behind
    holds a port and a database connection for as long as nobody notices.
    """
    ctypes.CDLL(None).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


class Replica:
    """One running front-door process: its origin, its marks directory, and how to kill it."""

    def __init__(self, process: "subprocess.Popen[bytes]", port: int, log: Path) -> None:
        """Wrap a started process."""
        self.process = process
        self.port = port
        self.log = log

    @property
    def base(self) -> str:
        """The replica's loopback origin."""
        return f"http://127.0.0.1:{self.port}"

    def kill(self) -> None:
        """SIGKILL it: no handler runs, nothing is flushed."""
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)

    def output(self) -> str:
        """What the process wrote to stdout and stderr, for a failure message."""
        return self.log.read_text(errors="replace")


def free_port() -> int:
    """An ephemeral loopback port, released for the child to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def replica_env(work: Path, **overrides: Any) -> dict[str, str]:
    """The environment of a replica on the test schema, with `overrides` as `CHEMCLAW_*` settings.

    `work` holds the replicas' marks and gate: a started turn writes a file in `work/marks`, and
    `work/gate` releases every parked turn. Keys already spelled `REPLICA_*` pass through as given.
    """
    env = dict(os.environ)
    (work / "marks").mkdir(exist_ok=True)
    env.update(
        {
            "CHEMCLAW_POSTGRES_DSN": settings.postgres_dsn,
            "CHEMCLAW_SESSION_STORE_DSN": settings.session_store_dsn,
            "CHEMCLAW_SESSION_STORE": "postgres",
            "CHEMCLAW_SERVICE_HOST": "127.0.0.1",
            "CHEMCLAW_LLM_ALLOW_LOOPBACK_GATEWAY": "true",
            "REPLICA_MARKS": str(work / "marks"),
            "REPLICA_GATE": str(work / "gate"),
            "REPLICA_TOKENS": "10",
        }
    )
    for key, value in overrides.items():
        name = key if key.startswith("REPLICA_") else f"CHEMCLAW_{key.upper()}"
        env[name] = str(value)
    return env


def _spawn(
    env: dict[str, str], module: str = "tests.replica_process", *, port_env: str = ""
) -> Replica:
    """Start one process of `module` on a free port; it is not ready until `_await_ready` says so.

    A front-door replica takes the port as its argument; a worker (`port_env` names the setting)
    serves its probes on it.
    """
    port = free_port()
    log = Path(tempfile.mkstemp(prefix="replica-", suffix=".log")[1])
    command = [sys.executable, "-m", module]
    if port_env:
        env = {**env, port_env: str(port)}
    else:
        command.append(str(port))
    with log.open("wb") as sink:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            preexec_fn=die_with_parent,
        )
    return Replica(process, port, log)


def _await_ready(started: Replica, path: str = "/healthz") -> None:
    """Block until the process answers `path`, or fail with what it wrote."""
    deadline = time.monotonic() + READY_SECONDS
    while True:
        if started.process.poll() is not None:
            raise RuntimeError(f"the replica exited at start:\n{started.output()}")
        try:
            if httpx.get(f"{started.base}{path}", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"the replica never answered {path}:\n{started.output()}")
        time.sleep(0.1)


@contextlib.contextmanager
def _running(started: list[Replica], ready_path: str) -> Iterator[list[Replica]]:
    """Wait for every process in `started` to answer `ready_path`; kill them all on exit."""
    try:
        for each in started:
            _await_ready(each, ready_path)
        yield started
    finally:
        for each in started:
            if each.process.poll() is None:
                each.process.send_signal(signal.SIGKILL)
                each.process.wait(timeout=10)
            each.log.unlink(missing_ok=True)


def replicas(env: dict[str, str], count: int) -> contextlib.AbstractContextManager[list[Replica]]:
    """Start `count` replicas together (their imports overlap), wait for all, kill them on exit."""
    return _running([_spawn(env) for _ in range(count)], "/healthz")


def workers(
    env: dict[str, str], modules: Sequence[str]
) -> contextlib.AbstractContextManager[list[Replica]]:
    """Start one Temporal worker per entry of `modules` (repeat a name for replicas of it).

    Each serves `/readyz` on its own port, and is ready once it answers. Killed on exit.
    """
    return _running(
        [_spawn(env, module, port_env="CHEMCLAW_WORKER_METRICS_PORT") for module in modules],
        "/readyz",
    )


@contextlib.contextmanager
def replica(env: dict[str, str]) -> Iterator[Replica]:
    """Start one replica, wait until it answers `/healthz`, and kill it on exit."""
    with replicas(env, 1) as (only,):
        yield only


def marks(work: Path) -> list[str]:
    """The names of the turns that have started, one per `hold` turn a replica began."""
    return sorted(path.name for path in (work / "marks").iterdir())
