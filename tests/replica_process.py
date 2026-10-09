"""One front-door replica as its own OS process, for tests that need real separation.

`python -m tests.replica_process <port>` serves the real `create_app` over the durable stores named
by the environment (`CHEMCLAW_*`: a test points two of these at one database). Identity is a
test header (`x-test-user`) put through the real rate-limit step; the model is a scripted turn:

- `hold …` records that it started (one file in `REPLICA_MARKS`) and parks until the `REPLICA_GATE`
  file exists;
- `boom …` fails mid-reply, the way a model call that raises does;
- anything else answers at once, reporting `REPLICA_TOKENS` tokens of usage.

`REPLICA_WRITE_DELAY` seconds are added before every cost-ledger and budget write, which makes the
window between "the client has the answer" and "the bookkeeping has landed" wide enough to hit
deterministically.
"""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Request

from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, _within_budget, require_principal
from tests.fakes_turn import Chunk, Piece, ScriptedTurn


class _Replica(ScriptedTurn):
    """The scripted model: answers, or parks at a file gate."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """Say it started if asked to hold, wait for the gate, then answer."""
        if message.startswith("hold"):
            marks = Path(os.environ["REPLICA_MARKS"])
            (marks / f"{os.getpid()}-{uuid.uuid4().hex}").write_text(message)
            gate = Path(os.environ["REPLICA_GATE"])
            while not await asyncio.to_thread(gate.exists):
                await asyncio.sleep(0.02)
        if message.startswith("boom"):
            raise RuntimeError("the model call failed")
        yield Chunk(text=f"answer to {message}", output_tokens=int(os.environ["REPLICA_TOKENS"]))


async def _as_header_user(request: Request) -> Principal:
    """The header's person, spent against the request budget exactly as `require_principal` does."""
    oid = request.headers["x-test-user"]
    return await _within_budget(Principal(oid=oid, upn=f"{oid}@corp", roles=frozenset({"chemist"})))


def _delay_writes(seconds: float) -> None:
    """Make the cost ledger and the durable budget window slow to write, by `seconds` each."""
    from chemclaw.agent.turn_cost_store import PostgresTurnCostSink
    from chemclaw.api import budget_store

    record: Any = PostgresTurnCostSink.record
    book: Any = budget_store.book

    async def slow_record(self: Any, cost: Any) -> None:
        await asyncio.sleep(seconds)
        await record(self, cost)

    async def slow_book(actor: str, tokens: int) -> Any:
        await asyncio.sleep(seconds)
        return await book(actor, tokens)

    PostgresTurnCostSink.record = slow_record  # type: ignore[method-assign]
    budget_store.book = slow_book


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """No connectors: the turn is the scripted model's."""
    return []


def main() -> None:
    """Serve one replica on the port given as the first argument."""
    delay = float(os.environ.get("REPLICA_WRITE_DELAY", "0"))
    if delay:
        _delay_writes(delay)
    app = create_app(graph_factory=_Replica().graph_factory, connector_factory=_no_connectors)
    app.dependency_overrides[require_principal] = _as_header_user
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


if __name__ == "__main__":
    main()
