"""Freezing the startup heap: what it does, and that both kinds of process do it before serving.

The conftest replaces the freeze for every other test, because it is process-wide; each test here
puts the real one back and thaws the heap again before it ends.
"""

import asyncio
import gc
import weakref
from collections.abc import Iterator
from typing import Any, cast

import pytest

from chemclaw.core import heap
from chemclaw.core.config import settings
from chemclaw.core.heap import freeze_startup_heap


@pytest.fixture
def thawed() -> Iterator[None]:
    """Unfreeze whatever a test froze, so the rest of the suite is collected normally."""
    gc.unfreeze()
    try:
        yield
    finally:
        gc.unfreeze()


def test_the_freeze_moves_the_heap_out_of_the_collector_and_skips_garbage(thawed: None) -> None:
    """Live objects are frozen; a cycle that was already garbage is collected, not frozen."""

    class _Node:
        partner: Any = None

    garbage = _Node()
    garbage.partner = garbage
    gone = weakref.ref(garbage)
    del garbage

    freeze_startup_heap("test")

    assert gc.get_freeze_count() > 0
    assert gone() is None, "startup's own cyclic garbage was frozen instead of freed"


def test_the_front_door_freezes_before_it_serves(
    monkeypatch: pytest.MonkeyPatch, thawed: None
) -> None:
    """By the time the lifespan yields, the heap the front door built is frozen."""
    from chemclaw.api import app as app_module

    async def _unchecked() -> None:
        return None

    monkeypatch.setattr(heap, "freeze_startup_heap", freeze_startup_heap)
    monkeypatch.setattr(app_module, "verify_deployment", _unchecked)

    def _no_connectors(_profile: str | None = None) -> list[Any]:
        return []

    app = app_module.create_app(connector_factory=_no_connectors)

    async def _serve() -> int:
        async with app.router.lifespan_context(app):
            return gc.get_freeze_count()

    assert asyncio.run(_serve()) > 0


def test_a_worker_freezes_before_its_first_poll(
    monkeypatch: pytest.MonkeyPatch, thawed: None
) -> None:
    """`serve_worker` freezes the heap before it starts the worker's polling."""
    from chemclaw.durable import serve as serve_module

    async def _unchecked() -> None:
        return None

    class _Count:
        count = 0

    class _Client:
        async def count_workflows(self, _query: str) -> _Count:
            return _Count()

    frozen_at_first_poll: list[int] = []

    class _Worker:
        """Returns from `run` at once, which `serve_worker` treats as the worker ending."""

        is_running = False
        client = _Client()

        async def run(self) -> None:
            frozen_at_first_poll.append(gc.get_freeze_count())

    monkeypatch.setattr(heap, "freeze_startup_heap", freeze_startup_heap)
    monkeypatch.setattr(settings, "worker_metrics_port", 0)
    monkeypatch.setattr(serve_module, "verify_deployment", _unchecked)
    asyncio.run(serve_module.serve_worker(cast(Any, _Worker()), component="test-worker"))
    assert frozen_at_first_poll and frozen_at_first_poll[0] > 0
