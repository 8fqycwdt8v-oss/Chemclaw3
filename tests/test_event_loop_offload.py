"""Synchronous CPU work must not run on the event loop that serves other requests.

A 50-user load test measured throughput flat at ~1.18 turns/s from 10 users to 50 — five times
the load for 1.7% more work — which is the signature of a single serialization point rather than
a resource limit. The front door and each connector are one uvicorn process on one event loop,
and the RDKit calls behind the chat tools are synchronous C++: while one turn depicts a molecule
or embeds a conformer, every other turn on that process is stopped.

These tests assert the property directly — the blocking call happens on a *different thread* than
the coroutine that awaited it — rather than measuring wall-clock, which would be flaky and would
not distinguish "fast" from "off the loop". Each one fails if the `asyncio.to_thread` hop is
removed.
"""

import asyncio
import threading
from typing import Any

import pytest
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw.connectors.chem.server import tools as chem_tools
from chemclaw.core.reagents import resolve_compound_name
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.xtb_spec import XtbSpec


def _thread_recording(target: Any, seen: list[int]) -> Any:
    """Wrap `target` so every call records the thread it ran on, then delegates unchanged."""

    def _spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.get_ident())
        return target(*args, **kwargs)

    return _spy


def test_render_structure_draws_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Depiction (2D coordinates + SVG rasterisation) is the heaviest RDKit call in `chem`."""
    seen: list[int] = []
    monkeypatch.setattr(
        rdMolDraw2D, "MolDraw2DSVG", _thread_recording(rdMolDraw2D.MolDraw2DSVG, seen)
    )

    async def _run() -> tuple[int, str]:
        return threading.get_ident(), await chem_tools.render_structure("CCO")

    loop_thread, svg = asyncio.run(_run())
    assert "<svg" in svg
    assert seen and all(thread != loop_thread for thread in seen)


def test_stoichiometry_table_weighs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A charge table is one RDKit parse plus a descriptor call per species."""
    seen: list[int] = []
    monkeypatch.setattr(
        chem_tools, "_molecular_weight", _thread_recording(chem_tools._molecular_weight, seen)
    )

    async def _run() -> tuple[int, chem_tools.ChargeTable]:
        # Water moved to the `solvents`/`volumes` path when densities landed: a species with a
        # density is charged by volume and the molar-equivalent path now rejects it outright.
        table = await chem_tools.stoichiometry_table("CCO", 46.0, [], [], ["water"], [1.0])
        return threading.get_ident(), table

    loop_thread, table = asyncio.run(_run())
    assert [row.smiles for row in table.rows] == ["CCO", "O"]
    assert seen and all(thread != loop_thread for thread in seen)


def test_resolve_compound_canonicalises_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised name falls through to an RDKit canonicalisation, not a dict lookup."""
    seen: list[int] = []
    monkeypatch.setattr(
        chem_tools, "resolve_compound_name", _thread_recording(resolve_compound_name, seen)
    )

    async def _run() -> int:
        assert await chem_tools.resolve_compound("c1ccccc1") is not None
        return threading.get_ident()

    loop_thread = asyncio.run(_run())
    assert seen and all(thread != loop_thread for thread in seen)


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
