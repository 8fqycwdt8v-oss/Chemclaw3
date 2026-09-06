"""One sink may not hold the drain, and a driver that hangs may not starve the ones after it.

`durable/publish_results.py` iterates the enabled sinks **sequentially**, and its module docstring
gives that shape a reason: *"two enabled destinations are two failure domains: one being
unreachable must not hold up the other."* That is true of the rows — one row per (sink, calc_ref) —
and it was false of the pass. The only ceiling over the loop was the activity's
`result_publish_timeout_seconds x len(sinks)`, one budget the first sink could drink entirely,
while the setting's own declaration calls it a per-`deliver` bound.

Measured on the unfixed seam with `alpha` hanging and `beta` healthy over eight passes: `beta` was
claimed **zero** times and its row sat at `attempts=0` with an empty `last_error`, so nothing even
distinguished "starved" from "nothing to send".

Driven through the real `registry.build`, because that is where the bound now lives: a sink is
bounded because the registry built it, not because a particular caller remembered to wrap it.
"""

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.publish import registry
from chemclaw.publish.driver import ResultSink, SinkUnavailableError
from chemclaw.publish.manifest import ResultSinkManifest
from chemclaw.publish.record import ResultRecord


class _Hanging:
    """A driver that never answers — a blackholed warehouse, a dropped SYN, a wedged endpoint."""

    def __init__(self, *, name: str, tenant_id: str, **_: Any) -> None:
        """Accept the seam's two mandatory keywords and ignore the rest."""
        self.name = name
        self.closed = False

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Never return."""
        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        """Never return here either — a driver that will not let go of its connection."""
        self.closed = True
        await asyncio.sleep(3600)


def hanging(**kwargs: Any) -> ResultSink:
    """The `module:callable` a probe manifest names."""
    return _Hanging(**kwargs)


def _manifest() -> ResultSinkManifest:
    """A manifest naming the hanging driver in this very module."""
    return ResultSinkManifest.model_validate(
        {
            "name": "hangprobe",
            "description": "a sink that never answers",
            "driver": f"{__name__}:hanging",
            "config": {},
        }
    )


@pytest.fixture
def bounded_sink(monkeypatch: pytest.MonkeyPatch) -> ResultSink:
    """A real, registry-built sink over the hanging driver, with a short per-sink ceiling."""
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")
    monkeypatch.setattr(settings, "result_publish_timeout_seconds", 0.5)
    return registry.build(_manifest())


def test_a_hanging_sink_gives_up_at_the_per_sink_ceiling(bounded_sink: ResultSink) -> None:
    """`deliver` must return control at `result_publish_timeout_seconds`, not hold the pass.

    Retryable, because the destination did not answer — which is also what puts the reason into
    `result_publications.last_error`, where an operator reads it. The unfixed seam returned control
    only when the whole activity timed out, having claimed the rows and marked none of them.
    """
    started = time.perf_counter()
    with pytest.raises(SinkUnavailableError) as outage:
        asyncio.run(bounded_sink.deliver([]))
    elapsed = time.perf_counter() - started

    assert "result_publish_timeout_seconds" in str(outage.value), (
        "the failure must name the knob that produced it, or an operator cannot raise it"
    )
    assert elapsed < 5.0, f"the per-sink ceiling did not bound the delivery ({elapsed:.1f}s)"


def test_a_sink_that_will_not_close_does_not_cost_the_next_one_its_pass(
    bounded_sink: ResultSink,
) -> None:
    """`aclose` is bounded too, and swallows its timeout.

    The drain calls it from a `finally` that has nothing to do with delivery, so a driver refusing
    to release a connection must not become the same starvation by another route — and must not
    turn a successful pass into a raised one either.
    """
    started = time.perf_counter()
    asyncio.run(bounded_sink.aclose())
    assert time.perf_counter() - started < 5.0, "an unbounded aclose starves the next sink"


def test_the_bound_is_the_seam_s_rather_than_a_caller_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sink `build` returns is bounded, so no caller can forget to wrap one.

    Stated as a property of the returned object rather than of the drain loop: the backfill CLI and
    any later caller get the guarantee for free, and the activity's `x len(sinks)` budget becomes
    the honest sum of N per-sink budgets rather than a pool.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")
    built = registry.build(_manifest())
    assert not isinstance(built, _Hanging), (
        "build() handed back the raw driver; the per-sink ceiling is then whatever the caller "
        "remembers to impose, which is how one hanging destination starved every other one"
    )
    assert isinstance(built, ResultSink)


def test_a_driver_that_is_not_a_sink_is_still_named_before_it_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Protocol check runs on the driver, so its error names the driver's own failure."""
    monkeypatch.setattr(settings, "manifest_driver_packages", "builtins")
    manifest = ResultSinkManifest.model_validate(
        {
            "name": "notasink",
            "description": "not a sink at all",
            "driver": "builtins:dict",
            "config": {},
        }
    )
    with pytest.raises(registry.ResultSinkError) as refusal:
        registry.build(manifest)
    assert "did not build a ResultSink" in str(refusal.value)
