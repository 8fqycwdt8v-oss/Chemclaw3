"""Synchronous CPU work must not run on the event loop that serves other requests.

A 50-user load test measured throughput flat at ~1.18 turns/s from 10 users to 50 — five times
the load for 1.7% more work — which is the signature of a single serialization point rather than
a resource limit. The front door and each connector are one uvicorn process on one event loop,
and the RDKit calls behind the chat tools are synchronous C++: while one turn embeds a conformer,
every other turn on that process is stopped.

**Three of these tests left with the `chem` server.** They covered `render_structure`,
`stoichiometry_table` and `resolve_compound`, which this repository no longer runs — the capability
is an MCP server in `Chemclaw3-mcp` now. The guard went with it rather than being deleted: that
repository's `Chemclaw3-mcp:servers/chem/tests/test_event_loop_offload.py` asserts the same
property against the
same tools, because a `to_thread` hop whose test stayed behind is one nobody would notice losing.
What remains here is what this process still runs on its own loop.

These tests assert the property directly — the blocking call happens on a *different thread* than
the coroutine that awaited it — rather than measuring wall-clock, which would be flaky and would
not distinguish "fast" from "off the loop". Each one fails if the `asyncio.to_thread` hop is
removed.
"""

import asyncio
import threading
from typing import Any

import pytest

from chemclaw.science.calc.store import InMemoryStore


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_the_rrho_arithmetic_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3N x 3N eigendecomposition is blocking work, and this coroutine shares its loop.

    The two blocking steps this test used to cover — embedding a molecule and deriving a cache key
    that shelled out to `xtb --version` — both left with the engines
    (`D-2026-08-16-the-physics-leaves-the-cache-stays`): the embed is a remote call and the key
    comes back from the server. What is left on this side is real work all the same. Turning a
    Hessian into a free energy diagonalizes a matrix that is 99x99 for a drug-sized molecule, once
    per refinement pass, inside the connector's single-loop MCP server and inside Temporal
    activities that are coroutines — so it has to be threaded, and the assertion is the thread it
    actually ran on rather than the presence of a call to `to_thread`.
    """
    from chemclaw.connectors.calc import compose
    from chemclaw.science.calc import thermo
    from tests.calc_server_fake import FakeCalcServer, install

    install(monkeypatch, FakeCalcServer())
    threads: list[int] = []
    monkeypatch.setattr(
        compose,
        "thermochemistry_from_hessian",
        _thread_recording(thermo.thermochemistry_from_hessian, threads),
    )

    async def _run() -> int:
        await compose.relax_to_minimum(InMemoryStore(), await compose.embed("CCO"), None)
        return threading.get_ident()

    loop_thread = asyncio.run(_run())
    assert threads and all(thread != loop_thread for thread in threads)


def test_gather_evidence_runs_its_sources_concurrently() -> None:
    """Independent retrievers are gathered, so the sweep costs the slowest source, not their sum.

    Two retrievers that each sleep are the honest model of "one reads the note tree, one queries
    Postgres": awaited in sequence the tool takes both delays, gathered it takes one. Asserted as
    overlap in time rather than a wall-clock threshold, so it is not timing-sensitive.
    """
    from chemclaw.agent import research_tools

    running = 0
    peak = 0

    class _SlowRetriever:
        """A retriever that reports how many of its peers were in flight alongside it."""

        source_id = "slow"
        # `SourceRetriever` declares `name`, and the fan-out reads it to label each branch's
        # contribution. This double predated that read and omitted it, which made it not actually a
        # `SourceRetriever` — filled in here rather than by making the sweep tolerant, because a
        # production path defending against an incomplete test double hides the incompleteness.
        name = "slow"

        async def retrieve(self, query: str, filters: dict[str, str]) -> list[Any]:
            """Sleep like a real I/O-bound source, tracking concurrent occupancy."""
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return []

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            research_tools, "_text_retrievers", lambda: [_SlowRetriever(), _SlowRetriever()]
        )
        assert asyncio.run(research_tools.gather_evidence("anything")) == []
    assert peak == 2
