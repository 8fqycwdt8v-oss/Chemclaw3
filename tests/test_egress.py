"""The LangSmith egress decision holds in this process, not only in the Helm chart.

`langsmith` is a hard dependency of `langchain-core` and enables itself from the environment. The
tests use the hostile ordering: the environment is made truthy after `chemclaw.core.config` is
imported and langsmith's env cache is cleared, which a pin that only wrote `os.environ` fails.
"""

import os
from collections.abc import Iterator
from typing import Any, cast

import pytest

# Imported first and deliberately: importing this package is what applies the pin, and every test
# below depends on that having already happened.
from chemclaw.core.config import settings  # noqa: F401  (imported for its import side effect)
from chemclaw.core.egress import pin_langsmith_egress

_TRACING_ENV_NAMES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def _clear_langsmith_env_cache() -> None:
    """Drop langsmith's cached view of the tracing environment variables.

    `get_env_var` is `lru_cache`d, so without this the environment set here is never read. `cast`
    because its `@overload` declaration hides the cache wrapper's attributes from the type checker.
    """
    from langsmith.utils import get_env_var

    cast(Any, get_env_var).cache_clear()


def _tracing_is_enabled() -> object:
    """Langsmith's own predicate, re-read from a cleared cache.

    Imported inside the function rather than at module scope so nothing here can hold a reference
    taken before the pin ran.
    """
    from langsmith.utils import tracing_is_enabled

    _clear_langsmith_env_cache()
    return tracing_is_enabled()


def _langchain_tracer_attached() -> bool:
    """Whether langchain would actually attach the tracer that does the sending.

    `tracing_is_enabled()` is the decision; this is the consequence that puts bytes on the wire.
    """
    from langchain_core.callbacks.manager import CallbackManager
    from langchain_core.tracers.langchain import LangChainTracer

    handlers = CallbackManager.configure().handlers
    return any(isinstance(handler, LangChainTracer) for handler in handlers)


@pytest.fixture(autouse=True)
def _restore_the_pin() -> Iterator[None]:
    """Restore the real pin after each test, whatever the test did.

    What the session is entitled to afterwards is this repository's decision,
    `pin_langsmith_egress`, not the saved values.
    """
    saved = {name: os.environ.get(name) for name in _TRACING_ENV_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        pin_langsmith_egress(allowed=False)
        _clear_langsmith_env_cache()


def test_a_truthy_environment_after_import_still_traces_nothing() -> None:
    """A truthy environment set after import still traces nothing.

    Only the process-wide `_GLOBAL_TRACING_ENABLED` that `pin_langsmith_egress` sets via
    `langsmith.configure` can make this False, because `tracing_is_enabled` consults it before the
    environment. An environ-only pin fails whenever the cache is warm, which importing `langchain`
    guarantees.
    """
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "true"

    assert _tracing_is_enabled() is False
    assert not _langchain_tracer_attached()


def test_allowing_tracing_does_not_override_the_operators_environment() -> None:
    """`allowed=True` hands the choice back to the operator's environment rather than making it.

    A truthy environment survives the call, as raw variables and as langsmith's verdict. The global
    fallback is cleared first, or the assertion would read the stale pin; the fixture restores it.
    """
    import langsmith

    langsmith.configure(enabled=None)
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "true"

    pin_langsmith_egress(allowed=True)

    assert [os.environ[name] for name in _TRACING_ENV_NAMES] == ["true", "true"]
    assert _tracing_is_enabled() is True
