"""Synchronous CPU work must not run on the event loop that serves other requests.

The front door and each connector are one process on one event loop, so blocking work stops every
other turn. The tests assert that the blocking call runs on a different thread than the awaiting
coroutine, rather than measuring wall clock; each fails if the `asyncio.to_thread` hop is removed.
The chem tools' equivalents live with the `chem` server in `Chemclaw3-mcp`.
"""

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from chemclaw.agent.subscriptions import Subscription
from chemclaw.science.calc.store import InMemoryStore


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_the_rrho_arithmetic_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RRHO arithmetic runs off the event loop.

    Turning a Hessian into a free energy diagonalizes a 3N x 3N matrix, inside the connector's
    single-loop server and inside coroutine activities. Asserted on the thread it ran on.
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

    Asserted as overlap in time rather than a wall-clock threshold.
    """
    from chemclaw.agent import research_tools

    running = 0
    peak = 0

    class _SlowRetriever:
        """A retriever that reports how many of its peers were in flight alongside it."""

        source_id = "slow"
        # `SourceRetriever` declares `name`, which the fan-out reads to label each branch; the
        # double supplies it rather than the production path tolerating its absence.
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

    The sampler wakes every 5 ms; actual wait minus requested wait is time the loop was held. That
    distinguishes a slow activity (fine) from one that stops the others sharing its loop.
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
    """`collect_digests` reads and matches the corpus off the event loop.

    `load_notes` and the O(subscriptions x notes) match are blocking, on a loop shared with other
    activities' heartbeats, including long searches that are costly to redeliver.
    """
    from chemclaw.durable import digest

    threads: list[int] = []

    def _slow_load(*args: Any, **kwargs: Any) -> list[Any]:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS)
        return []

    # One subscription, not zero: with none, `_match_corpus` returns before touching the corpus, so
    # a zero-subscription fixture would assert the offload of work the activity does not do.
    async def _one_subscription() -> list[Any]:
        return [Subscription(id=1, owner="u-1", query="suzuki")]

    monkeypatch.setattr(digest, "load_notes", _slow_load)
    monkeypatch.setattr(digest, "all_subscriptions", _one_subscription)
    monkeypatch.setattr(digest, "conflict_index", lambda *a, **k: {})

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
    """The memory note builders run off the event loop.

    Each ends in `load_notes` after CPU-bound clustering over the reaction corpus. Threading at the
    activity boundary covers both and keeps `memory/jobs.py` a pure sync module.
    """
    from chemclaw.durable import memory_jobs

    threads: list[int] = []

    def _slow_build(*args: Any, **kwargs: Any) -> list[Any]:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS)
        return []

    async def _no_reactions() -> memory_jobs.CorpusRead:
        return memory_jobs.CorpusRead(reactions=[], complete=True)

    monkeypatch.setattr(memory_jobs, "read_corpus", _no_reactions)
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


def _drop_directory(root: Any, files: int, payload: dict[str, Any]) -> Any:
    """`files` copies of one export payload in `root`, which is what a drop directory is."""
    import json as _json

    for index in range(files):
        (root / f"export-{index:03d}.json").write_text(_json.dumps(payload), encoding="utf-8")
    return root


#: How many export files each scan below reads. Three, because the block is injected per file
#: rather than measured off real parsing: what is being asserted is that the *scan* leaves the loop
#: schedulable, and one file would not distinguish a threaded scan from a lucky yield.
_EXPORT_FILES = 3


def test_the_eln_json_adapter_scans_its_drop_directory_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The `eln-json` adapter scans its drop directory off the event loop.

    The glob, reads and parses ran as one block on the worker loop that also carries heartbeats and
    health endpoints, so a large corpus starves the heartbeat. The per-file cost is injected, so the
    assertion is the loop stall, not disk speed.
    """
    import chemclaw.ingest.eln.json_adapter as json_adapter

    _drop_directory(
        tmp_path,
        _EXPORT_FILES,
        {
            "id": "e",
            "timestamp": "2026-01-01T00:00:00Z",
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
        },
    )
    threads: list[int] = []
    real = json_adapter._parse_timestamp

    def _slow(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS / _EXPORT_FILES)
        return real(*args, **kwargs)

    monkeypatch.setattr(json_adapter, "_parse_timestamp", _slow)
    adapter = json_adapter.JsonExportAdapter(str(tmp_path))

    stall_ms, loop_thread = _worst_loop_stall(
        lambda: adapter.fetch_new_entries(datetime(2020, 1, 1, tzinfo=UTC))
    )

    assert len(threads) == _EXPORT_FILES, "the drop directory was never read"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"the ELN adapter held the worker's loop for {stall_ms:.1f} ms of a "
        f"{_BLOCK_SECONDS * 1000:.0f} ms directory scan"
    )
    assert all(thread not in loop_thread for thread in threads), (
        "the ELN adapter read its drop directory on the event loop"
    )


def test_the_ord_adapter_scans_its_drop_directory_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The same seam and the same scan, over the heavier files: 937.4 ms at 10,000 messages."""
    import chemclaw.ingest.eln.ord_adapter as ord_adapter

    _drop_directory(
        tmp_path,
        _EXPORT_FILES,
        {"reaction_id": "r", "provenance": {"record_created": {"time": {"value": "2026-01-01"}}}},
    )
    threads: list[int] = []
    real = ord_adapter._created_at

    def _slow(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS / _EXPORT_FILES)
        return real(*args, **kwargs)

    monkeypatch.setattr(ord_adapter, "_created_at", _slow)
    adapter = ord_adapter.OrdJsonAdapter(str(tmp_path))

    stall_ms, loop_thread = _worst_loop_stall(
        lambda: adapter.fetch_new_entries(datetime(2020, 1, 1, tzinfo=UTC))
    )

    assert len(threads) == _EXPORT_FILES, "the drop directory was never read"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"the ORD adapter held the worker's loop for {stall_ms:.1f} ms of a "
        f"{_BLOCK_SECONDS * 1000:.0f} ms directory scan"
    )
    assert all(thread not in loop_thread for thread in threads), (
        "the ORD adapter read its drop directory on the event loop"
    )


def test_the_commitment_export_is_read_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The portfolio mirror reads its whole export every sync: 595.1 ms at 10,000 files."""
    import chemclaw.ingest.commitments.json_export as json_export
    from chemclaw.ingest.commitments.models import Commitment

    _drop_directory(
        tmp_path,
        _EXPORT_FILES,
        {"commitments": [{"external_id": "c", "title": "t", "owner": "o"}]},
    )
    threads: list[int] = []

    def _slow(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS / _EXPORT_FILES)
        return Commitment(*args, **kwargs)

    monkeypatch.setattr(json_export, "Commitment", _slow)
    source = json_export.JsonCommitmentExport(name="portfolio", path=str(tmp_path))

    stall_ms, loop_thread = _worst_loop_stall(lambda: source.fetch_commitments(None))

    assert len(threads) == _EXPORT_FILES, "the export was never read"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"the commitment export held the worker's loop for {stall_ms:.1f} ms of a "
        f"{_BLOCK_SECONDS * 1000:.0f} ms read"
    )
    assert all(thread not in loop_thread for thread in threads), (
        "the commitment export was read on the event loop"
    )


def test_the_bo_activities_fit_their_surrogate_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BO activities fit their surrogate off the event loop.

    A GP fit is pure synchronous CPU. Drives the two activities `BoCampaignWorkflow` runs every
    round, with BoFire stubbed: the property is where the fit runs, not its result.
    """
    import chemclaw.connectors.bo.activities as activities
    from chemclaw.science.bo.problem import (
        Candidate,
        ContinuousParameter,
        Objective,
        Observation,
        OptimizationProblem,
    )

    threads: list[int] = []

    def _slow(*args: Any, **kwargs: Any) -> list[Candidate]:
        threads.append(threading.get_ident())
        time.sleep(_BLOCK_SECONDS / 2)
        return [Candidate(params={"t": 1.0 * len(threads)})]

    monkeypatch.setattr(activities, "initial_candidates", _slow)
    monkeypatch.setattr(activities, "propose_candidates", _slow)

    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="t", lower=0.0, upper=100.0)],
        objectives=[Objective(name="y", direction="maximize")],
    )
    observations = [
        Observation(params={"t": float(i)}, value=float(i), provenance="measured") for i in range(5)
    ]

    async def _both() -> None:
        await activities.propose_initial(problem, 3, 1)
        await activities.propose_next(problem, observations, 1, 1)

    stall_ms, loop_thread = _worst_loop_stall(_both)

    assert len(threads) == 2, "neither BoFire call ran"
    assert stall_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"the campaign held its loop for {stall_ms:.1f} ms of a {_BLOCK_SECONDS * 1000:.0f} ms fit"
    )
    assert all(thread not in loop_thread for thread in threads), (
        "the campaign fitted its surrogate on the event loop"
    )
