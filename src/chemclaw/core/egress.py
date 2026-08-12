"""Deciding, in this process, whether conversation content may leave for LangSmith.

Why this exists: `langsmith` is in the runtime closure whether anyone wants it or not — a hard
requirement of `langchain-core`, pulled again by `deepagents` — and it turns *itself* on from
ambient environment, with no repo code involved at any point. Measured on langsmith 0.10.17:
with `LANGSMITH_TRACING=true` in the environment, `langsmith.utils.tracing_is_enabled()` returns
True and `langchain_core.callbacks.manager.CallbackManager.configure()` attaches a
`LangChainTracer`, which posts prompts and completions to api.smith.langchain.com. The run even
emits `LangSmithMissingAPIKeyWarning` — it had already decided to send before noticing it had no
credential.

D-2026-08-11 declined LangSmith for the production path, and the pin enforcing that lived in
exactly one place: `deploy/helm/chemclaw/templates/_helpers.tpl`. So the decision held for a
Helm-installed pod and for nothing else — `make chat`, `make connectors`, a hand-started worker,
local dev, CI, and a plain `docker run` of the shipped image were all unguarded. A policy that
only one of six ways of starting the process obeys is a policy about that deployment tool, not
about the system. This module is the same decision made where every process makes it.

**Why the pin needs both halves, measured rather than assumed.**
`langsmith.utils.get_env_var` is `functools.lru_cache`d, so the first read of
`LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` is the only one that ever consults the process
environment, and importing `langchain` warms that cache. Measured: with the cache warm on a
truthy value, writing `os.environ` afterwards leaves `tracing_is_enabled()` True and the tracer
attached — an environ-only pin is a **no-op in exactly the ordering that occurs in practice**,
while looking correct in a test that sets the variable late.
`langsmith.configure(enabled=False)` sets the process-wide `_GLOBAL_TRACING_ENABLED`, which
`tracing_is_enabled` consults *before* the cached variable, so it wins regardless of import
order. The `os.environ` write is still made, and not as a belt-and-braces gesture: a stdio
connector subprocess inherits this environment and runs its own interpreter, where a global set
in this one means nothing at all. One half covers this process, the other covers its children,
and neither covers both.
"""

import os

import langsmith

# The two names langsmith accepts for the same question. `LANGCHAIN_TRACING_V2` is the older
# spelling and is still honoured — `get_env_var` searches the `LANGSMITH` namespace and then the
# `LANGCHAIN` one — so pinning only the new name leaves the old one live.
_TRACING_ENV_NAMES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def pin_langsmith_egress(*, allowed: bool) -> None:
    """Settle LangSmith tracing for this process and every subprocess it spawns.

    Called once from `chemclaw.core.config` immediately after the settings singleton is built —
    that module is the one import every entrypoint makes (`api/app.py`, `cli/chat.py`,
    `cli/connectors_dev.py`, `connectors/server_entry.py`, `durable/background_worker.py`), which
    is what makes this a property of the system rather than of one launcher.

    `allowed=False` (the default posture) disables tracing globally *and* writes both environment
    names, for the reason the module docstring measures: the global is the only thing that beats a
    warm `lru_cache`, and the environment is the only thing a child process can see.

    `allowed=True` does **not** enable tracing. It only stops Chemclaw overriding the environment,
    leaving the operator's configuration — including their `LANGSMITH_API_KEY` and endpoint — to
    decide. Enabling egress is not a thing this function should be able to do by itself.

    **It overrides ambient environment deliberately, and `setdefault` would be wrong here.** An
    ambient `LANGSMITH_TRACING=true` is indistinguishable, from inside this process, from a
    deliberate one: it may have come from a base image layer, a CI runner's defaults, or a `.env`
    copied from a machine where somebody was debugging. A `setdefault` would honour every one of
    those, which means honouring none of them *as a decision* — the deployment would trace or not
    trace based on which of its ancestors happened to export a variable. `langsmith_tracing_allowed`
    is where that choice is made, so it is the only input read here.
    """
    if allowed:
        return
    # Beats the warm lru_cache in this interpreter (`tracing_is_enabled` reads the global first).
    langsmith.configure(enabled=False)
    # And carries the same answer into stdio connector subprocesses, which inherit this environ
    # and start an interpreter where the global above does not exist.
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "false"
