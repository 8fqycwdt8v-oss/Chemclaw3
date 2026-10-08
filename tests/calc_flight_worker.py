"""One process of the calculation single-flight tests: N concurrent callers missing one key.

Run as `python -m tests.calc_flight_worker <spec.json>`. The spec names the calculation, the file
every real computation appends a line to (the count the tests assert on), how long a computation
takes, when to start (so two processes race) and how many concurrent callers to run. Prints one JSON
line: each caller's `[result, was_cached]` or the failure it received.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from chemclaw.core import db
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import CalculationKey, cached_compute


def _note_start(spec: dict[str, Any]) -> None:
    """Record one real computation: a line in the counter file, and the announce marker."""
    # One short O_APPEND write is atomic across processes.
    with Path(spec["counter"]).open("a") as counter:
        counter.write(f"{os.getpid()}\n")
    if spec.get("announce"):
        Path(spec["announce"]).write_text("computing")


async def _caller(spec: dict[str, Any], key: CalculationKey) -> list[Any]:
    """One caller: miss, compute or await, report what it got."""

    async def compute() -> dict[str, Any]:
        _note_start(spec)
        await asyncio.sleep(spec["hold_seconds"])
        if spec.get("fail"):
            raise ValueError(spec["fail"])
        return {"value": spec["value"], "computed_by": os.getpid()}

    try:
        result, cached = await cached_compute(
            PostgresStore(), key, compute, wait_seconds=spec.get("wait_seconds")
        )
    except Exception as exc:
        return ["error", type(exc).__name__, str(exc)]
    return [result, cached]


async def _main(spec: dict[str, Any]) -> list[list[Any]]:
    """Wait for the start time, then run the callers concurrently."""
    key = CalculationKey(
        calc_type="flight", calc_version="1", input_hash=spec["input_hash"], params_hash="p"
    )
    async with db.pooling():
        delay = spec["start_at"] - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return list(await asyncio.gather(*(_caller(spec, key) for _ in range(spec["callers"]))))


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main(json.loads(Path(sys.argv[1]).read_text())))))
