"""`python -m chemclaw.cli.bench_backend_rpc` — time `calculation_key` over MCP and over `/rpc`.

`calculation_key` is the call core makes before every cache lookup, so its round trip is the
floor of a cache hit. Against the calc server `CHEMCLAW_CALC_SERVER_URL` names, with the bearer in
`CHEMCLAW_CALC_SERVER_TOKEN_ENV`, one call at a time after a warm-up:

- `mcp_session_per_call`: a fresh MCP session per call (`initialize`, the call, teardown), the
  `calc_transport=mcp` cost;
- `rpc_pooled`: `POST /rpc/calculation_key` on the process's pooled client, the `rpc` cost;
- `cache_hit_mcp`, `cache_hit_rpc`: `cached_remote` on a stored result in the real result store
  (Postgres, so it needs the database the deployment uses and writes that one cached result), i.e.
  one session, one key call and a lookup, which is what a cache hit costs this process.

Prints one JSON document of milliseconds and optionally writes it. A measurement, never an
assertion: where the server runs (loopback, in-cluster) decides the numbers.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chemclaw_contracts.calc import CalculationKeyRequest, PredictSolubilityRequest

from chemclaw.connectors.calc import remote
from chemclaw.core import backend_rpc, db, mcp_session
from chemclaw.core.config import settings
from chemclaw.science.calc.postgres_store import default_store

REQUEST = PredictSolubilityRequest(smiles="CCO")
KEY_REQUEST = CalculationKeyRequest(tool=REQUEST.tool_name, arguments=REQUEST.wire())
WARMUP_CALLS = 20


def summary(seconds: list[float]) -> dict[str, float]:
    """Milliseconds: the median, the 95th percentile, the mean and the fastest."""
    ordered = sorted(seconds)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "calls": float(len(ordered)),
        "p50_ms": round(statistics.median(ordered) * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 2),
        "min_ms": round(ordered[0] * 1000, 2),
    }


async def timed(call: Callable[[], Awaitable[Any]], calls: int) -> list[float]:
    """Warm up, then time `calls` sequential awaits of `call`."""
    for _ in range(WARMUP_CALLS):
        await call()
    out: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        await call()
        out.append(time.perf_counter() - started)
    return out


async def measure(calls: int, session_calls: int) -> dict[str, dict[str, float]]:
    """Time the four rows against the configured calc server."""
    url, token_env = settings.calc_server_url, settings.calc_server_token_env
    timeout = settings.calc_server_timeout_seconds

    async def mcp_per_call() -> None:
        async with mcp_session.open_session(
            url, token_env=token_env, timeout_seconds=timeout
        ) as session:
            await mcp_session.invoke(session, KEY_REQUEST.tool_name, KEY_REQUEST.wire())

    session = backend_rpc.RpcSession(backend_rpc.rpc_base_url(url), token_env, timeout)

    async def rpc_pooled() -> None:
        await session.invoke(KEY_REQUEST.tool_name, KEY_REQUEST.wire())

    store = default_store()

    async def cache_hit() -> None:
        await remote.cached_remote(store, REQUEST)

    out = {
        "mcp_session_per_call": summary(await timed(mcp_per_call, session_calls)),
        "rpc_pooled": summary(await timed(rpc_pooled, calls)),
    }
    for transport in ("mcp", "rpc"):
        settings.calc_transport = transport
        await remote.cached_remote(store, REQUEST)  # the miss that stores the result
        count = session_calls if transport == "mcp" else calls
        out[f"cache_hit_{transport}"] = summary(await timed(cache_hit, count))
    await backend_rpc.close_pools_of_this_loop()
    return out


async def pooled(calls: int, session_calls: int) -> dict[str, dict[str, float]]:
    """`measure` with the database pooled, as a deployed process runs it."""
    async with db.pooling():
        return await measure(calls, session_calls)


def main() -> None:
    """Measure and print."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=300)
    parser.add_argument("--session-calls", type=int, default=60)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    report = {
        "request": KEY_REQUEST.wire(),
        "server": settings.calc_server_url,
        "results": asyncio.run(pooled(args.calls, args.session_calls)),
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
