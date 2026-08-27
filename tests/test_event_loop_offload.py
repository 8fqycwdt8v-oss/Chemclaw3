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
import contextlib
import threading
import time
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
        assert asyncio.run(research_tools.gather_evidence("anything")).chunks == []
    assert peak == 2


def _worst_loop_stall(coro_factory: Any) -> tuple[float, list[int]]:
    """Run a coroutine while sampling the loop, returning the worst stall in ms and the loop thread.

    The sampler wakes every 5 ms; whatever it *actually* waited, minus what it asked for, is the
    time the loop was held by something that never yielded. That is the number the audit measured
    (1,223.8 ms inline vs 27.0 ms threaded for the identical corpus work) and the only one that
    distinguishes "this activity is slow" — which is fine, activities are — from "this activity
    stops the other seven sharing its loop", which is not.
    """
    stalls: list[float] = []

    async def _sample(stop: asyncio.Event) -> None:
        while not stop.is_set():
            before = time.perf_counter()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.005)
            stalls.append((time.perf_counter() - before - 0.005) * 1000)

    async def _run() -> list[int]:
        stop = asyncio.Event()
        sampler = asyncio.create_task(_sample(stop))
        await asyncio.sleep(0.02)  # let the sampler settle before the work starts
        await coro_factory()
        stop.set()
        await sampler
        return [threading.get_ident()]

    loop_thread = asyncio.run(_run())
    return max(stalls, default=0.0), loop_thread


# How long the stand-in corpus read blocks for. Long enough that an inline call is unmistakable
# against scheduler noise, short enough to keep the test fast; the assertions below are stated as
# fractions of it rather than as absolute milliseconds.
_BLOCK_SECONDS = 0.3


def test_the_digest_reads_and_matches_the_corpus_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`collect_digests` shares its loop with seven other activities, and held it for the parse.

    `load_notes` is a recursive `rglob` + `stat` + YAML frontmatter parse of every note, and the
    match pass after it is O(subscriptions x notes) of pure Python. Both ran inline in a
    `background-jobs` coroutine that `worker_max_concurrent_activities=8` shares with — among
    others — `beating()`'s heartbeat timers for a CREST search that costs hours if its heartbeat is
    missed. Measured by the audit on a 2,000-note corpus: 1,223.8 ms of loop stall inline against
    27.0 ms through `to_thread`, for identical work.
    """
    from chemclaw.durable import digest

    threads: list[int] = []

    def _slow_load(*args: Any, **kwargs: Any) -> list[Any]:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS)
        return []

    async def _no_subscriptions() -> list[Any]:
        return []

    monkeypatch.setattr(digest, "load_notes", _slow_load)
    monkeypatch.setattr(digest, "all_subscriptions", _no_subscriptions)

    stall_ms, loop_thread = _worst_loop_stall(digest.collect_digests)

    assert threads, "the corpus read never happened"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"the digest held the worker's loop for {stall_ms:.1f} ms of a "
        f"{_BLOCK_SECONDS * 1000:.0f} ms corpus read"
    )
    assert all(thread not in loop_thread for thread in threads), (
        "collect_digests read the note corpus on the event loop"
    )


@pytest.mark.parametrize(
    "activity_name",
    [
        "build_campaign_notes_activity",
        "build_playbook_notes_activity",
        "build_optimization_notes_activity",
    ],
)
def test_the_memory_note_builders_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, activity_name: str
) -> None:
    """The same corpus read, reached three-at-a-time by `MemorySynthesisWorkflow`'s fan-out.

    Each builder ends in `_with_supersedes`, which calls `load_notes`; the clustering in front of
    it is pure CPU over the whole reaction corpus. Threading at the *activity* boundary covers both
    and leaves `memory/jobs.py` the pure sync module its layer says it should be.
    """
    from chemclaw.durable import memory_jobs

    threads: list[int] = []

    def _slow_build(*args: Any, **kwargs: Any) -> list[Any]:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS)
        return []

    async def _no_reactions() -> list[Any]:
        return []

    monkeypatch.setattr(memory_jobs, "all_reactions", _no_reactions)
    monkeypatch.setattr(
        memory_jobs, activity_name.removesuffix("_activity"), _slow_build, raising=True
    )

    stall_ms, loop_thread = _worst_loop_stall(getattr(memory_jobs, activity_name))

    assert threads, "the builder never ran"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"{activity_name} held the worker's loop for {stall_ms:.1f} ms of a "
        f"{_BLOCK_SECONDS * 1000:.0f} ms build"
    )
    assert all(thread not in loop_thread for thread in threads), (
        f"{activity_name} built its notes on the event loop"
    )
