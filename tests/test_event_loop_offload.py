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
from chemclaw.science.calc.xtb_spec import XtbSpec


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_electronic_properties_embed_and_key_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding the molecule and deriving its cache key are both blocking, and both were on-loop.

    The key matters as much as the embed: `calc_version()` shells out to `xtb --version` on its
    first call in a process, so a cold worker paid a `subprocess.run` on the loop that serves
    every Temporal task poll and heartbeat.
    """
    from chemclaw.science.calc import xtb_props

    embeds: list[int] = []
    keys: list[int] = []
    monkeypatch.setattr(
        xtb_props, "_property_structure", _thread_recording(xtb_props._property_structure, embeds)
    )
    monkeypatch.setattr(XtbSpec, "cache_key", _thread_recording(XtbSpec.cache_key, keys))

    async def _run() -> int:
        await xtb_props.run_cached_properties(InMemoryStore(), "CCO")
        return threading.get_ident()

    loop_thread = asyncio.run(_run())
    assert embeds and all(thread != loop_thread for thread in embeds)
    assert keys and all(thread != loop_thread for thread in keys)


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
