"""Launching `tests/replica_process.py`: real front-door processes on one database.

A test that claims something about separate replicas (a limit that holds across them, a write that
survives a kill) needs separate processes; threads in one process share the in-memory state the
claim is about. The processes inherit the test's redirected DSN, so they share the test schema.
"""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from chemclaw.core.config import settings


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


def _free_port() -> int:
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


def _spawn(env: dict[str, str]) -> Replica:
    """Start one replica process; it is not ready until `_await_ready` says so."""
    port = _free_port()
    log = Path(tempfile.mkstemp(prefix="replica-", suffix=".log")[1])
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.replica_process", str(port)],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    return Replica(process, port, log)


def _await_ready(started: Replica) -> None:
    """Block until the replica answers `/healthz`, or fail with what it wrote."""
    deadline = time.monotonic() + 90
    while True:
        if started.process.poll() is not None:
            raise RuntimeError(f"the replica exited at start:\n{started.output()}")
        try:
            if httpx.get(f"{started.base}/healthz", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"the replica never answered /healthz:\n{started.output()}")
        time.sleep(0.1)


@contextlib.contextmanager
def replicas(env: dict[str, str], count: int) -> Iterator[list[Replica]]:
    """Start `count` replicas together (their imports overlap), wait for all, kill them on exit."""
    started = [_spawn(env) for _ in range(count)]
    try:
        for each in started:
            _await_ready(each)
        yield started
    finally:
        for each in started:
            if each.process.poll() is None:
                each.process.send_signal(signal.SIGKILL)
                each.process.wait(timeout=10)
            each.log.unlink(missing_ok=True)


@contextlib.contextmanager
def replica(env: dict[str, str]) -> Iterator[Replica]:
    """Start one replica, wait until it answers `/healthz`, and kill it on exit."""
    with replicas(env, 1) as (only,):
        yield only


def marks(work: Path) -> list[str]:
    """The names of the turns that have started, one per `hold` turn a replica began."""
    return sorted(path.name for path in (work / "marks").iterdir())
