"""A checkpointer outage is reported as retryable storage trouble, and counted.

Decision: `D-2026-08-27-a-refusal-is-not-a-crash`. `api/runner._classify` decides what a person is
told from the exception type (`ConnectionError`/`TimeoutError`); the checkpointer runs on its own
pool, bypassing `core/db`'s translation, so `SchemaStampedSaver` translates outages itself and
counts failed checkpoint writes.
"""

import asyncio
import logging
from typing import Any, cast

import psycopg
import psycopg_pool
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from chemclaw.agent.checkpointer import STATE_CHANNELS_KEY, SchemaStampedSaver
from chemclaw.api.runner import _classify
from chemclaw.core.metrics import METRICS


def test_the_measurement_that_makes_the_translation_necessary() -> None:
    """`PoolTimeout` is neither of the two types the front door's classifier tests.

    Pinned because the translation rests on it; if psycopg changes this, the translation is
    redundant.
    """
    assert not issubclass(psycopg_pool.PoolTimeout, ConnectionError)
    assert not issubclass(psycopg_pool.PoolTimeout, TimeoutError)
    # And it *is* caught by what the saver catches, which is the other half of the pairing —
    # `OperationalError`, the same class `core/db.py` catches, and not the two levels above it.
    assert issubclass(psycopg_pool.PoolTimeout, psycopg.OperationalError)
    assert issubclass(psycopg_pool.PoolClosed, psycopg.OperationalError)


def test_a_failed_checkpoint_write_is_counted_and_retryable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The translation, the count, and the answer a chemist ends up with.

    Driven end to end through `_classify`, because the property is "the person is told to retry".
    `aput` is patched at the superclass, so no database is needed.
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


async def test_a_working_write_still_stamps_the_channels_and_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is a `try` around the existing write, not a change to what it writes.

    The channel stamp is the saver's reason to exist; losing it would break every mid-turn resume.
    """
    before = METRICS.value("chemclaw_degraded_total")
    seen: list[CheckpointMetadata] = []

    async def _record(self: Any, config: Any, checkpoint: Any, metadata: Any, versions: Any) -> Any:
        seen.append(metadata)
        return config

    monkeypatch.setattr(AsyncPostgresSaver, "aput", _record)
    saver = SchemaStampedSaver.__new__(SchemaStampedSaver)

    await saver.aput(
        {"configurable": {"thread_id": "session-42"}},
        Checkpoint(),  # type: ignore[typeddict-item]
        CheckpointMetadata(),
        {},
    )

    assert STATE_CHANNELS_KEY in seen[0]
    assert METRICS.value("chemclaw_degraded_total") == before


def test_a_missing_table_is_not_translated_into_retry_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing table is not translated into retry-forever.

    Only pool and connection failures are translated (as in `core/db.py`). A `ProgrammingError` such
    as `UndefinedTable` must not become a retryable `storage_unavailable`, since retrying cannot fix
    it.
    """
    assert not issubclass(psycopg.errors.UndefinedTable, psycopg.OperationalError), (
        "the premise of this case: a missing table is a ProgrammingError, not an outage"
    )

    async def _no_such_table(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.errors.UndefinedTable('relation "checkpoints" does not exist')

    monkeypatch.setattr(AsyncPostgresSaver, "aput", _no_such_table)
    saver = SchemaStampedSaver.__new__(SchemaStampedSaver)

    async def _write() -> None:
        await saver.aput(
            {"configurable": {"thread_id": "session-42"}},
            Checkpoint(),  # type: ignore[typeddict-item]
            CheckpointMetadata(),
            {},
        )

    with pytest.raises(psycopg.errors.UndefinedTable) as raised:
        asyncio.run(_write())
    # It reaches the front door as what it is: a fault, not a wait.
    assert _classify(raised.value) == ("internal", False)


@pytest.mark.parametrize("statement", ["aget_tuple", "aput_writes", "alist"])
def test_every_statement_on_this_pool_translates_its_outage_not_only_the_write(
    statement: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every statement on this pool translates its outage, not only the write.

    A `PoolTimeout` on `aget_tuple` (the load at turn start) is as likely as at write time.
    Parametrised because the property is "every statement".
    """

    async def _pool_is_saturated(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.0 sec")

    async def _saturated_stream(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30.0 sec")
        yield  # pragma: no cover - unreachable; makes this an async generator

    monkeypatch.setattr(
        AsyncPostgresSaver,
        statement,
        _saturated_stream if statement == "alist" else _pool_is_saturated,
    )
    saver = SchemaStampedSaver.__new__(SchemaStampedSaver)
    config = cast(RunnableConfig, {"configurable": {"thread_id": "session-42"}})

    async def _run() -> None:
        if statement == "aget_tuple":
            await saver.aget_tuple(config)
        elif statement == "aput_writes":
            await saver.aput_writes(config, [("channel", "value")], "task-1")
        else:
            async for _stored in saver.alist(config):
                pass

    with pytest.raises(ConnectionError) as raised:
        asyncio.run(_run())
    assert _classify(raised.value) == ("storage_unavailable", True)
    assert "session-42" in str(raised.value)
