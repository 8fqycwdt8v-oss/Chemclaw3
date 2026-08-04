"""An unreachable Temporal broker says so, once, for every durable tool.

The defect these guard (live run 2026-08-03): `connect()` raised temporalio's raw
`RuntimeError('Failed client connect: … tonic::transport::Error …')`, MAF collapsed it to
"Error: Function failed." for the model, and the model — told nothing else —
**wrote an entire development report by hand** and presented it as having entered the PR-gate.
The generator never ran.

Every test here drives the real `connect()` against a real closed port: the thing under test is
precisely what temporalio does on a refused connection, so substituting it would prove nothing.
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

    `_CLIENT` must start empty or the warm path returns a cached client and never connects; the
    lock is replaced because `asyncio.Lock` binds to the first event loop that acquires it, and
    each test runs its own `asyncio.run`.
    """
    monkeypatch.setattr(settings, "temporal_address", _closed_address())
    monkeypatch.setattr(temporal_client, "_CLIENT", None)
    monkeypatch.setattr(temporal_client, "_CONNECT_LOCK", asyncio.Lock())


def test_an_unreachable_broker_is_named_and_its_consequence_stated(
    unreachable_broker: None,
) -> None:
    """The message must name Temporal, say nothing was queued, and disown the user's input.

    All three carry weight. Naming the subsystem stops the model guessing at a chemistry cause;
    "nothing was queued" is what keeps a chemist from waiting on a job that does not exist; and
    saying it is an outage stops the retry-with-different-SMILES storm the live run produced.
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
    """An outage must leave `connect()` able to retry: no cached client, no held lock.

    `connect()` caches its client in a module singleton behind a lock, so a failure that assigned
    a broken value would make the outage permanent for the process, and one that left the lock
    acquired would hang every later caller instead of failing them.
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
    """It must not be catchable as `ChemclawError`/`ValueError` — see `chemclaw.core.errors`.

    The reject-and-continue boundaries that catch bad data would otherwise swallow an outage as a
    poison record, and `chemclaw.durable.publish` would classify it non-retryable. Asserted on the
    real raised instance rather than on the class hierarchy, because the hierarchy is what a future
    edit would change.
    """
    with pytest.raises(SubsystemUnavailableError) as excinfo:
        asyncio.run(temporal_client.connect())

    assert not isinstance(excinfo.value, ChemclawError)
    assert not isinstance(excinfo.value, ValueError)
