"""The LangSmith egress decision holds in *this* process, not only in the Helm chart.

`langsmith` is in the runtime closure whether anyone wants it or not (a hard requirement of
`langchain-core`, pulled again by `deepagents`) and enables itself from ambient environment:
measured, `LANGSMITH_TRACING=true` alone makes `tracing_is_enabled()` return True and
`CallbackManager.configure()` attach a `LangChainTracer` that posts prompts and completions to
api.smith.langchain.com. D-2026-08-11 declined that for the production path, and the pin used to
live only in `deploy/helm/chemclaw/templates/_helpers.tpl` — so it governed a Helm-installed pod
and nothing else.

These tests are written against the *hostile* ordering on purpose (see the first one's docstring):
the environment is made truthy **after** `chemclaw.core.config` has been imported and the
`lru_cache` behind langsmith's env read is cleared, which is the state a fix that only wrote
`os.environ` would fail in — and it is the state that occurs in practice, because importing
`langchain` warms that cache before any Chemclaw code runs.
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

    Without this the environment these tests set is never read at all: `get_env_var` is
    `functools.lru_cache`d, so the first read of each name is the only one that consults
    `os.environ` for the life of the process — which is precisely why the pin cannot be an
    `os.environ` write alone. `cast` because langsmith declares `get_env_var` with `@overload`,
    which hides the cache wrapper's attributes from the type checker while the runtime object
    plainly has them.
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

    `tracing_is_enabled()` is the decision; this is the consequence, and it is the one that puts
    bytes on the wire. Asserted separately because they are two different pieces of library code
    and only the second one is the harm.
    """
    from langchain_core.callbacks.manager import CallbackManager
    from langchain_core.tracers.langchain import LangChainTracer

    handlers = CallbackManager.configure().handlers
    return any(isinstance(handler, LangChainTracer) for handler in handlers)


@pytest.fixture(autouse=True)
def _restore_the_pin() -> Iterator[None]:
    """Leave the process exactly as pinned, whatever a test did to get there.

    Both tests below deliberately put langsmith into a state the rest of the session must not
    inherit — a truthy environment, and (in the second) the global fallback cleared. Restoring the
    real pin rather than the saved values is the honest teardown: what the session is entitled to
    afterwards is the decision this repo makes, which is what `pin_langsmith_egress` is.
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
    """The hostile ordering: the environment says trace, and nothing traces anyway.

    Both names are set truthy **after** `chemclaw.core.config` was imported, and the `lru_cache`
    behind langsmith's env read is then cleared — so the library is looking at an environment that
    unambiguously asks for tracing, exactly as it is in a pod whose base image exported the
    variable before Python started. The only thing that can produce False here is the process-wide
    `_GLOBAL_TRACING_ENABLED` that `pin_langsmith_egress` sets via `langsmith.configure`, because
    `tracing_is_enabled` consults it *before* the environment.

    This is the assertion a pin that only wrote `os.environ` cannot pass, and the reason the
    ordering is written this way rather than the friendlier one: measured on langsmith 0.10.17,
    an environ-only pin leaves `tracing_is_enabled()` True and the tracer attached whenever the
    cache is already warm — which importing `langchain` guarantees.
    """
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "true"

    assert _tracing_is_enabled() is False
    assert not _langchain_tracer_attached()


def test_allowing_tracing_does_not_override_the_operators_environment() -> None:
    """`allowed=True` hands the choice back rather than making it — the other half of the contract.

    The setting is not an on-switch; it is Chemclaw declining to override. So the check is that a
    deliberately truthy environment survives the call, both as the raw variables (what a stdio
    connector subprocess inherits) and as langsmith's own verdict.

    The global fallback is cleared first (`configure(enabled=None)`, langsmith's documented "fall
    back to environment variables"), because the module-level pin already set it False — without
    that reset the assertion would pass for the wrong reason, reading a stale global instead of
    proving this call left the environment alone. The fixture puts the real pin back afterwards.
    """
    import langsmith

    langsmith.configure(enabled=None)
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "true"

    pin_langsmith_egress(allowed=True)

    assert [os.environ[name] for name in _TRACING_ENV_NAMES] == ["true", "true"]
    assert _tracing_is_enabled() is True
