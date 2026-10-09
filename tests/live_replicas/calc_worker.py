"""The `calc` bundle's real Temporal worker over a stand-in for the physics server.

`python -m tests.live_replicas.calc_worker` polls `connector-calc` exactly as the deployed worker
does (`chemclaw.connectors.calc.worker`): the same activities, the same Postgres result store and
claim ledger. Only the far end of `calc_session` is replaced, by `tests/calc_server_fake.py` made
slow and made to write what it was asked: one line per tool call in `LANE_CALC_LOG`, shared by
every worker, as `<pid> <tool> <key digest or ->`. A computation is a line with a key; the lane
counts those across processes. `LANE_CALC_HOLD` seconds is how long a computation takes.

The first `calculation_key` of each worker waits until `LANE_CALC_PARTIES` workers have made theirs,
so every worker has missed the cache before any of them can have computed: the contention the lane
asserts on is arranged, not hoped for.
"""

import asyncio
import os
import time
from typing import Any

import pytest

from chemclaw.connectors.calc import (
    activities as _activities,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.calc import (
    workflows as _workflows,  # noqa: F401 — registration side effect
)
from chemclaw.connectors.worker import main
from chemclaw.core.ids import stable_hash
from tests.calc_server_fake import FakeCalcServer, install

#: How long a worker waits for the others before it fails the job it holds, loudly.
_RENDEZVOUS_SECONDS = 60.0


async def _rendezvous() -> None:
    """Return once `LANE_CALC_PARTIES` workers (this one included) have reached this point."""
    arrivals = os.environ["LANE_CALC_ARRIVALS"]
    os.makedirs(arrivals, exist_ok=True)
    os.close(os.open(f"{arrivals}/{os.getpid()}", os.O_WRONLY | os.O_CREAT))
    deadline = time.monotonic() + _RENDEZVOUS_SECONDS
    while len(os.listdir(arrivals)) < int(os.environ["LANE_CALC_PARTIES"]):
        if time.monotonic() > deadline:
            raise RuntimeError("the other calc workers never took a job: no contention to measure")
        await asyncio.sleep(0.05)


class _RecordingServer(FakeCalcServer):
    """The fake server, taking `LANE_CALC_HOLD` seconds over each keyed computation."""

    def _key_digest(self, tool: str, arguments: dict[str, Any]) -> str:
        """The digest of the cache key `tool` would store under, or `-` for a call with none."""
        if tool == "calculation_key":
            return "-"
        try:
            key = self._identity(tool, arguments)["key"]
        except ValueError:
            return "-"
        return "-" if key is None else stable_hash(key)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Write the call down before answering it, and take the computation's time."""
        if name == "calculation_key":
            await _rendezvous()
        digest = self._key_digest(name, arguments)
        # One short O_APPEND write is atomic across processes.
        descriptor = os.open(os.environ["LANE_CALC_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(descriptor, f"{os.getpid()} {name} {digest}\n".encode())
        finally:
            os.close(descriptor)
        if digest != "-":
            await asyncio.sleep(float(os.environ["LANE_CALC_HOLD"]))
        return await super().call_tool(name, arguments)


if __name__ == "__main__":
    install(pytest.MonkeyPatch(), _RecordingServer())
    main("calc")
