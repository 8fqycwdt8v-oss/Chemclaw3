"""The harness loop-predicate types, declared here rather than imported from MAF's private module.

`chemclaw.agent.loop_cap` and `chemclaw.agent.plan_gate` each wrap the harness loop's
`should_continue` predicate, and both annotated that wrapping with
`agent_framework._harness._loop.ShouldContinueCallable` / `ShouldContinueResult` — a **private**
module of a dependency required as `agent-framework-core>=1.11.0` with no upper bound. A patch
release that moves either name is an `ImportError` at process start of *both* the front door and the
worker, which is the worst place to discover a dependency's refactor.

The reason that import can simply go away is what those two names are. Measured against the
installed 1.11.0:

    ShouldContinueResult   = 'bool | tuple[bool, str | None]'
    ShouldContinueCallable = Callable[..., 'ShouldContinueResult | Awaitable[ShouldContinueResult]']

They are **type aliases and nothing else** — no class, no runtime behaviour, no identity anything
compares against. MAF accepts our predicate structurally: it awaits whatever the callable returns
and reads the result. So importing them bought type annotations and a startup-time failure mode,
and declaring them buys the same annotations and no failure mode.

**The cost of declaring them is silent divergence**, and that is what
`tests/test_harness_types.py` exists to remove: it asserts these definitions still match MAF's,
whenever the private module can be imported. A shape change therefore fails a test — loudly, in CI,
naming what changed — instead of failing an import in production. That is the trade the whole
substitution is for: the same signal, moved off the startup path.

This is deliberately *not* pinned around with `agent-framework-core<1.12`. A pin would freeze the
package's security updates to buy protection against a private module that nothing here imports any
more, and it would still say nothing about a shape change *within* 1.11.
"""

from collections.abc import Awaitable, Callable
from typing import TypeAlias

__all__ = ["ShouldContinueCallable", "ShouldContinueResult"]

# What the harness loop's predicate may answer: keep going, or stop with an optional reason.
ShouldContinueResult: TypeAlias = bool | tuple[bool, str | None]

# The predicate itself. `...` for the parameters because MAF calls it with keyword arguments it
# chooses, and both wrappers here accept `**kwargs: Any` for exactly that reason; the return may be
# sync or async, and MAF awaits it either way.
ShouldContinueCallable: TypeAlias = Callable[
    ..., ShouldContinueResult | Awaitable[ShouldContinueResult]
]
