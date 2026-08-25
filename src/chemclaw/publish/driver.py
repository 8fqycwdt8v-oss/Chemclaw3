"""The narrow seam a result sink implements — a Protocol, and nothing else.

This module imports no database client and no third-party package, deliberately. It is what lets
the projection, the outbox and the SQL construction be exercised in CI against a fake, on a machine
with no warehouse and no vendor driver installed — the same property
`ingest/eln/warehouse/driver.py` buys for the inbound direction, and for the same reason.

**One method, and it takes a batch.** A sink handed one record at a time would
make the drain's round-trip count equal its row count, and every real target — a warehouse, a REST
endpoint — is cheaper per row in batches. A driver that genuinely wants one at a time loops.

**`deliver` must be idempotent.** The outbox retries, and a redelivery of a batch that partially
landed must converge rather than duplicate. Every primary key in the shipped schema is a content
hash precisely so that an upsert keyed on it is a no-op the second time.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from chemclaw.core.errors import ChemclawError
from chemclaw.publish.record import ResultRecord


class SinkUnavailableError(ConnectionError):
    """The sink could not be reached, or failed for a reason that may not recur.

    **A `ConnectionError`, deliberately not a `ChemclawError`** — and that is the whole retry
    contract rather than a taxonomy preference. This tree splits the two the same way everywhere:
    `ChemclawError` subclasses `ValueError` and `durable/publish.py` lists them as non-retryable by
    class name, because bad data fails identically forever; an *unreachable* dependency raises
    `ConnectionError` and is retried. `ingest/eln/warehouse/driver.py` makes exactly this split
    between its query error and an unreachable warehouse, for exactly this reason.

    So the distinction against `SinkRejectedError` is not descriptive: it decides whether the
    outbox tries again.
    """


class SinkRejectedError(ChemclawError):
    """The sink refused this content and will refuse it identically on every retry.

    A `ChemclawError` (so a `ValueError`), which `durable/publish.py` marks non-retryable by class
    name — a record naming a column the site has not created fails the same way forever, and
    burning a retry budget on it only delays an operator seeing the message.
    """


@runtime_checkable
class ResultSink(Protocol):
    """A destination computed results are published to. One per enabled sink manifest."""

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Write `records`, idempotently.

        Raises `SinkUnavailableError` when the destination could not be reached and the attempt is
        worth repeating; `SinkRejectedError` when the content itself is the problem. Returning
        normally means every record in the batch is durable at the far end — a driver that cannot
        promise that for a partial batch must raise rather than return.
        """
        ...
