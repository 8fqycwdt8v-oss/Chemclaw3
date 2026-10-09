"""The `calc` bundle's real Temporal worker over a stand-in for the physics server.

`python -m tests.live_replicas.calc_worker` polls `connector-calc` exactly as the deployed worker
does (`chemclaw.connectors.calc.worker`): the same activities, the same Postgres result store and
claim ledger. Only the far end of `calc_session` is replaced, by `tests/calc_server_fake.py` made
slow and made to write what it was asked: one line per tool call in `LANE_CALC_LOG`, shared by
every worker, as `<pid> <tool> <key digest or ->`. A computation is a line with a key; the lane
counts those across processes. `LANE_CALC_HOLD` seconds is how long a computation takes.
"""

import asyncio
import os
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
