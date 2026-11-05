"""The narrow seam a result sink implements: a Protocol, and nothing else.

Imports no database client or third-party package, so projection, outbox and SQL construction
are testable against a fake with no vendor driver installed. One method, taking a batch (every
real target is cheaper per row in batches). `deliver` must be idempotent: the outbox retries, and
content-hash keys make a repeated upsert a no-op.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from chemclaw.core.errors import ChemclawError
from chemclaw.publish.record import ResultRecord


class SinkUnavailableError(ConnectionError):
    """The sink could not be reached, or failed for a reason that may not recur.

    A `ConnectionError`, deliberately not a `ChemclawError`: that split is the retry contract.
    `durable/publish.py` treats `ChemclawError` subclasses as non-retryable by class name, and
    retries an unreachable dependency.
    """


class SinkRejectedError(ChemclawError):
    """The sink refused this content and will refuse it identically on every retry.

    A `ChemclawError`, which `durable/publish.py` marks non-retryable, so an operator sees the
    message without a retry budget being burned first. The drain retires the refused record at
    once (`durable/publish_results.py`).
    """


class SinkMisconfiguredError(SinkRejectedError):
    """The sink as configured refuses every record: its schema, credential or address is wrong.

    About the destination, not the record, so the drain books it batch-wide like an outage and
    retires nothing early: the rows deliver once an operator fixes the sink.
    """


@runtime_checkable
class ResultSink(Protocol):
    """A destination computed results are published to. One per enabled sink manifest."""

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Write `records`, idempotently.

        Raises `SinkUnavailableError` when the attempt is worth repeating, `SinkRejectedError`
        when the content is the problem and `SinkMisconfiguredError` when the sink itself is.
        Returning normally means every record is durable at the far end; a driver that cannot
        promise that for a partial batch must raise.
        """
        ...

    async def aclose(self) -> None:
        """Release whatever the sink is holding. Called after every drain pass.

        The drain builds a sink per run, so anything held open must be closed here. Must be safe to
        call twice, and on a sink that never connected.
        """
        ...
