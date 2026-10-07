"""An unreachable Temporal broker says so, once, for every durable tool.

Without a clear message the model is told nothing and may fabricate the job's output. Every test
drives the real `connect()` against a real closed port, since temporalio's behaviour on a refused
connection is the thing under test.
"""

import asyncio
import socket

import pytest

from chemclaw.core import temporal_client
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError, SubsystemUnavailableError


def _closed_address() -> str:
    """A `host:port` nothing is listening on, so a connect attempt is refused immediately.

    Bound and released rather than hard-coded, so the test cannot collide with whatever else this
    machine happens to be running (a developer's own `make up` Temporal included).
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"127.0.0.1:{port}"


@pytest.fixture
def unreachable_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `connect()` at a closed port, with a fresh singleton and lock for this test.

    `_CLIENT` must start empty or the cached client skips connecting; the lock is replaced because
    `asyncio.Lock` binds to the first event loop that acquires it.
    """
    monkeypatch.setattr(settings, "temporal_address", _closed_address())
    monkeypatch.setattr(temporal_client, "_CLIENT", None)
    monkeypatch.setattr(temporal_client, "_CONNECT_LOCK", asyncio.Lock())


def test_an_unreachable_broker_is_named_and_its_consequence_stated(
    unreachable_broker: None,
) -> None:
    """The message names Temporal, says nothing was queued, and disowns the user's input.

    Naming the subsystem stops the model guessing at a chemistry cause, "nothing was queued" stops a
    chemist waiting on a nonexistent job, and calling it an outage stops retries with different
    input.
    """
    with pytest.raises(SubsystemUnavailableError) as excinfo:
        asyncio.run(temporal_client.connect())

    message = str(excinfo.value)
    assert "Temporal" in message
    assert "nothing was queued" in message
    assert "not a problem with the request" in message
    # Written for a chemist: the address, the port and the driver text stay on `__cause__`.
    assert settings.temporal_address not in message
    assert "tonic" not in message


def test_the_underlying_transport_error_is_kept_as_the_cause(unreachable_broker: None) -> None:
    """The operator's half of the failure must survive — it is the only place the address lives."""
    with pytest.raises(SubsystemUnavailableError) as excinfo:
        asyncio.run(temporal_client.connect())

    cause = excinfo.value.__cause__
    assert cause is not None
    assert settings.temporal_address in str(cause)


def test_a_failed_connect_does_not_poison_the_singleton_or_the_lock(
    unreachable_broker: None,
) -> None:
    """An outage leaves `connect()` able to retry: no cached client, no held lock.

    A cached broken client would make the outage permanent for the process; a held lock would hang
    every later caller.
    """

    async def connect_twice() -> tuple[Exception, Exception]:
        errors: list[Exception] = []
        for _ in range(2):
            try:
                await temporal_client.connect()
            except SubsystemUnavailableError as exc:
                errors.append(exc)
            assert temporal_client._CLIENT is None  # nothing broken was cached
            assert not temporal_client._CONNECT_LOCK.locked()  # released on the failure path
        return errors[0], errors[1]

    first, second = asyncio.run(connect_twice())
    # Two genuine attempts, not one failure replayed: distinct exception objects, each with its
    # own live transport cause.
    assert first is not second
    assert first.__cause__ is not None and second.__cause__ is not None


def test_the_outage_error_is_not_bad_data(unreachable_broker: None) -> None:
    """The outage error is not catchable as `ChemclawError`/`ValueError`.

    Bad-data boundaries would otherwise swallow it as a poison record and `durable.publish` would
    classify it non-retryable. Asserted on the real raised instance.
    """
    with pytest.raises(SubsystemUnavailableError) as excinfo:
        asyncio.run(temporal_client.connect())

    assert not isinstance(excinfo.value, ChemclawError)
    assert not isinstance(excinfo.value, ValueError)
