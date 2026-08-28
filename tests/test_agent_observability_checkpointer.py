"""A checkpointer outage told the chemist not to retry, and moved no counter at all.

The decision is `D-2026-08-27-a-refusal-is-not-a-crash`. `api/runner._classify` decides what a
person is told from the exception's *type*, and it tests `ConnectionError` and `TimeoutError`.
`core/db.connection()` translates a pool failure into `ConnectionError` for exactly that reason —
and the checkpointer runs on its own autocommit pool, deliberately (three measured reasons in
`agent/checkpointer.py`), which bypasses that translation.

So the one Postgres pool that is not `core/db`'s was the one whose outage produced
`("internal", False)`: "internal error, do not retry", about the most retryable failure this system
has. Nothing counted a checkpoint write failure either, and mid-turn that is silent loss of the
turn's state.
"""

import asyncio
import logging
from typing import Any

import psycopg
import psycopg_pool
import pytest
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from chemclaw.agent.checkpointer import STATE_CHANNELS_KEY, SchemaStampedSaver
from chemclaw.api.runner import _classify
from chemclaw.core.metrics import METRICS


def test_the_measurement_that_makes_the_translation_necessary() -> None:
    """`PoolTimeout` is neither of the two types the front door's classifier tests.

    Pinned rather than described, because the whole fix rests on it: if psycopg ever made
    `PoolTimeout` a `TimeoutError`, the translation would be redundant and this test is where that
    is noticed instead of the code quietly doing something twice.
    """
    assert not issubclass(psycopg_pool.PoolTimeout, ConnectionError)
    assert not issubclass(psycopg_pool.PoolTimeout, TimeoutError)
    # And it *is* caught by what the saver catches, which is the other half of the pairing.
    assert issubclass(psycopg_pool.PoolTimeout, psycopg.Error)


def test_a_failed_checkpoint_write_is_counted_and_retryable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The translation, the count, and the answer a chemist ends up with.

    Driven end to end through `_classify` — which this workstream does not edit — because the
    property that matters is not "a different exception type is raised", it is "the person is told
    to try again". The saver is constructed with no pool: `aput` is patched at the superclass, so
    nothing here needs a database to prove what happens when one is unreachable.
    """
    before = METRICS.value("chemclaw_degraded_total")

    async def _pool_is_saturated(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.0 sec")

    monkeypatch.setattr(AsyncPostgresSaver, "aput", _pool_is_saturated)
    saver = SchemaStampedSaver.__new__(SchemaStampedSaver)

    async def _write() -> None:
        await saver.aput(
            {"configurable": {"thread_id": "session-42"}},
            Checkpoint(),  # type: ignore[typeddict-item]
            CheckpointMetadata(),
            {},
        )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ConnectionError) as raised:
            asyncio.run(_write())

    # The remedy the chemist is offered, which is the whole point of the type change.
    assert _classify(raised.value) == ("storage_unavailable", True)
    # The original is kept as the cause, so a log or a debugger still names `PoolTimeout`.
    assert isinstance(raised.value.__cause__, psycopg_pool.PoolTimeout)
    assert "session-42" in str(raised.value)
    assert METRICS.value("chemclaw_degraded_total") == before + 1
    assert 'chemclaw_degraded_total{subsystem="checkpointer"}' in METRICS.render()
    assert "degraded[checkpointer]" in caplog.text


def test_a_working_write_still_stamps_the_channels_and_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is a `try` around the existing write, not a change to what it writes.

    `SchemaStampedSaver`'s reason to exist is the channel stamp (`agent/checkpointer.py`), and a
    guard that quietly stopped stamping would break every mid-turn resume in the fleet without
    failing anything else.
    """
    before = METRICS.value("chemclaw_degraded_total")
    seen: list[CheckpointMetadata] = []

    async def _record(self: Any, config: Any, checkpoint: Any, metadata: Any, versions: Any) -> Any:
        seen.append(metadata)
        return config

    monkeypatch.setattr(AsyncPostgresSaver, "aput", _record)
    saver = SchemaStampedSaver.__new__(SchemaStampedSaver)

    async def _write() -> None:
        await saver.aput(
            {"configurable": {"thread_id": "session-42"}},
            Checkpoint(),  # type: ignore[typeddict-item]
            CheckpointMetadata(),
            {},
        )

    asyncio.run(_write())

    assert STATE_CHANNELS_KEY in seen[0]
    assert METRICS.value("chemclaw_degraded_total") == before
