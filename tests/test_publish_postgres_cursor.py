"""The Postgres sink cursor adapts and maps errors identically on its single and batched paths.

`executemany` used to carry its own copy of `execute`'s `Jsonb` comprehension and its own copy of
the error mapping, so an adaptation added to one would make the batched drain fail and fall back
to row-by-row replay. Both now go through `_adapted` and `_mapped_errors`; these tests drive both
paths over a fake psycopg cursor and assert the same answers, so a divergence is red.
"""

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from chemclaw.ingest.eln.warehouse.driver import WarehouseQueryError
from chemclaw.publish.drivers.postgres import _PostgresCursor


class _Recording:
    """A stand-in psycopg cursor that records what it was handed, or raises `error`."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.seen: list[Any] = []

    async def execute(self, sql: str, params: Any) -> None:
        if self.error is not None:
            raise self.error
        self.seen.append(params)

    async def executemany(self, sql: str, params_seq: Any) -> None:
        if self.error is not None:
            raise self.error
        self.seen.extend(params_seq)


def _cursor(fake: _Recording) -> _PostgresCursor:
    return _PostgresCursor(fake)  # type: ignore[arg-type]


def test_both_paths_wrap_documents_and_leave_scalars_alone() -> None:
    """A dict or list becomes `Jsonb`; anything else passes through unchanged, on both paths."""
    row = [{"a": 1}, [1, 2], "text", 3]
    single, batched = _Recording(), _Recording()
    asyncio.run(_cursor(single).execute("SQL", row))
    asyncio.run(_cursor(batched).executemany("SQL", [row, row]))
    for params in [single.seen[0], *batched.seen]:
        assert [type(value) for value in params] == [Jsonb, Jsonb, str, int]
        assert params[2:] == ["text", 3]


@pytest.mark.parametrize("batched", [False, True])
def test_a_programming_error_is_a_query_error_and_a_lost_server_is_not(batched: bool) -> None:
    """Non-retryable by class for a bad statement; the connection loss passes through as itself."""

    def run(error: BaseException) -> None:
        cursor = _cursor(_Recording(error))
        if batched:
            asyncio.run(cursor.executemany("SQL", [[1]]))
        else:
            asyncio.run(cursor.execute("SQL", [1]))

    with pytest.raises(WarehouseQueryError, match="UndefinedColumn"):
        run(psycopg.errors.UndefinedColumn("no such column"))
    with pytest.raises(psycopg.OperationalError):
        run(psycopg.OperationalError("server closed the connection"))
