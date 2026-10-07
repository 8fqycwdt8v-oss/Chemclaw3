"""Deciding, in this process, whether conversation content may leave for LangSmith.

`langsmith` is a hard dependency of `langchain-core` and enables itself from ambient environment
(`LANGSMITH_TRACING`), posting prompts and completions off-site. LangSmith is declined, so every
process settles it here rather than only in the Helm chart.

The pin needs two halves. `langsmith.utils.get_env_var` is `lru_cache`d and importing langchain
warms it, so writing `os.environ` later is a no-op in this process;
`langsmith.configure(enabled=False)` sets the global that `tracing_is_enabled` consults first. The
`os.environ` write is still needed for child processes (stdio connectors), which run their own
interpreter.
"""

import os

import langsmith

# Both names langsmith accepts; it still honours the older `LANGCHAIN_TRACING_V2`, so pinning only
# the new name leaves the old one live.
_TRACING_ENV_NAMES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def pin_langsmith_egress(*, allowed: bool) -> None:
    """Settle LangSmith tracing for this process and every subprocess it spawns.

    Called once from `chemclaw.core.config` after the settings singleton is built, the import every
    entrypoint makes. `allowed=False` disables the global and writes both environment names.
    `allowed=True` does not enable tracing; it only stops overriding the operator's environment.
    Ambient environment is overridden rather than `setdefault`ed because an inherited
    `LANGSMITH_TRACING=true` is indistinguishable from a deliberate one; `langsmith_tracing_allowed`
    is the only input.
    """
    if allowed:
        return
    # Beats the warm lru_cache in this interpreter (`tracing_is_enabled` reads the global first).
    langsmith.configure(enabled=False)
    # And carries the same answer into stdio connector subprocesses, which inherit this environ and
    # start an interpreter where the global above does not exist.
    for name in _TRACING_ENV_NAMES:
        os.environ[name] = "false"
