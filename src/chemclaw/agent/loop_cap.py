"""Make the model loop's runaway cap observable, so a capped turn stops looking finished.

A loop that stops at its iteration cap and **returns normally, emitting nothing** is externally
identical to one that finished its work. That silence cost twice under the framework this layer was
first built on. A deployment had no signal to alert on (`docs/planning/BACKLOG.md`), and
`chemclaw.evals.autonomy.runaway_rate` was reduced to inferring a runaway from *residue*: an answer
sent while the plan still held unchecked steps. Residue cannot tell "abandoned a step" from
"correctly deferred to a durable job", because a turn that defers correctly leaves exactly the same
trace — an open todo — behind.

**The counting is upstream's; only the observation is ours.** `ModelCallLimitMiddleware` already
enforces exactly this cap, and it keeps its per-run counter as
`run_model_call_count: NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]` — the same
untracked channel this module used to declare by hand, which is where that shape was copied from in
the first place. What upstream does not supply is the *record*: the counter carries
`PrivateStateAttr` (`OmitFromSchema(input=True, output=True)`), so it is absent from what `ainvoke`
returns, and it is `UntrackedValue`, so it is absent from the checkpoint too. Between the two there
is nowhere left to read "was this turn capped" from. So `CappedModelCallLimit` subclasses the
middleware, delegates the decision to it, and writes the one field upstream cannot: `loop_capped`.

The earlier first-party counter is gone with the hand-written half. Its enforcement half was a
second implementation of upstream's, and the two counts agree exactly — with `run_limit = N` both
let the model run `N` times and stop the run on call `N + 1`, because upstream checks in
`before_model` and increments in `after_model` while the old hook checked and incremented in the
same `before_model`. `tests/test_langgraph_stream.py` pins that against a compiled graph rather than
against the arithmetic.

**Two readers, because they ask from different places.** `loop_capped(state)` reads the flag off
the state a finished run *returns* — the untracked channel is absent from `get_state()`, by
design — which is what a test or a template step holds. `loop_hit_cap()` reads a contextvar the
hook marks on its way out, which is what `chemclaw.api.runner` holds — a streaming driver never
gets the final state back. The carrier is a contextvar holding a *mutable* record, for
the reasons `chemclaw.core.turn_signals` gives for its buffer: it is task-local (concurrent turns
cannot see each other's loops), it is empty off the request path (CLI, tests), and it is mutated
rather than rebound — so the mark is visible to the runner even when the stream is driven from a
task of its own.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoopWatch:
    """One turn's cap mark — `True` once the loop was stopped by its iteration cap."""

    capped: bool = False


_watch: ContextVar[_LoopWatch | None] = ContextVar("chemclaw_loop_watch", default=None)


def begin_loop_watch() -> object:
    """Start watching this turn's loop decisions; returns a token for `end_loop_watch`."""
    return _watch.set(_LoopWatch())


def end_loop_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _watch.reset(token)  # type: ignore[arg-type]


def loop_hit_cap() -> bool:
    """Whether this turn's model loop was stopped by its iteration cap.

    `False` off the request path, which is what makes this safe to ask unconditionally: no watch,
    no cap. The state-side answer is `loop_capped`; this is the one a streaming driver can reach.
    """
    watch = _watch.get()
    return watch is not None and watch.capped


class CappedModelCallLimit(ModelCallLimitMiddleware[Any]):
    """`ModelCallLimitMiddleware` that also *records* the cap it enforces.

    Upstream decides; this adds the two marks nothing upstream can leave behind — the `loop_capped`
    state field and the ambient watch — because both of upstream's counters are unreadable by the
    time anyone wants to ask. See the module docstring.

    Ending the run rather than raising (`exit_behavior="end"`, upstream's default): the answer the
    last iteration managed still goes out, and a surface marks it partial (`chemclaw.api.runner`
    does this off `loop_hit_cap`). `exit_behavior="error"` would discard work a chemist is entitled
    to see.
    """

    # `can_jump_to` is not decoration, it is the edge, and re-declaring it on an override is not
    # optional. **Without it the cap is inert**, and inert in the worst way: the hook runs, decides
    # correctly and returns `{"jump_to": "end"}` on every call after the limit — and the graph goes
    # on looping, because `before_model`'s conditional edge is *built from this declaration* and an
    # override that drops it drops the edge. Measured on the first-party version at a cap of 1: the
    # hook fired five times and said "end" four times while four further model/tool round-trips
    # completed anyway. That is why a unit test on the hook proves nothing here and
    # `tests/test_langgraph_stream.py` asserts against a compiled graph.
    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Delegate the cap decision upstream, and record it when it fires."""
        decision = super().before_model(state, runtime)
        if decision is None:
            return None
        logger.warning("the model loop hit its %d-iteration cap", self.run_limit)
        record_loop_cap()
        # `loop_capped` is written here and nowhere else. Upstream's own counters cannot answer the
        # question later: `run_model_call_count` is stripped from the output by `PrivateStateAttr`
        # and never checkpointed, and `thread_model_call_count` counts the *session*, not the turn.
        return {**decision, "loop_capped": True}

    # `abefore_model` is deliberately not overridden: upstream's implementation is
    # `return self.before_model(state, runtime)`, so it already dispatches to the override above.
    # Re-declaring it here would be a second copy of the same delegation, and one that could drift.


def loop_cap_middleware() -> CappedModelCallLimit:
    """Build the runaway cap for one graph, reading the limit at build time.

    A factory rather than a module-level instance because the limit is configuration:
    `settings.harness_max_loop_iterations` is ENV-overridable and the tests set it per case, and an
    instance frozen at import would answer with whatever was in the environment when the module was
    first imported.

    `run_limit`, not `thread_limit`: the cap bounds one *turn*. A thread limit would count a whole
    checkpointed session and stop the fourth turn of a long conversation for the sins of the first
    three.
    """
    return CappedModelCallLimit(run_limit=settings.harness_max_loop_iterations)


def record_loop_cap() -> None:
    """Mark this turn's watch, so the runner can see a cap it cannot read off the state.

    `chemclaw.api.runner` decides whether to emit `loop_cap_reached` and increment
    `chemclaw_turn_loop_caps_total` by calling `loop_hit_cap()`. It has no other way to ask: a
    compiled graph's final state is not something the streaming driver is handed back, so
    `loop_capped(state)` — the authoritative reader — is unreachable from there.

    Without this mark a capped turn was externally identical to a finished one: no error event, no
    counter, nothing for a surface to mark the answer partial with. That is the very defect this
    module exists to fix, reintroduced one layer up by leaving the runner with no reader at all.
    """
    watch = _watch.get()
    if watch is not None:
        # Mutated rather than rebound, for the reason the module docstring gives: the runner must
        # see it even when the stream is driven from a task of its own.
        watch.capped = True


def loop_capped(state: Any) -> bool:
    """Whether this turn's model loop was stopped by its cap — **read, not inferred**.

    The authoritative answer, and a different kind of answer from `loop_hit_cap`. The framework
    this layer was first built on offered no hook on its cap — it short-circuited the loop
    predicate once the limit was reached — so the only signal available was the shape of the *last
    decision the loop asked for*: "it wanted another iteration, and something other than the
    predicate stopped it". That inference was sound and had a hole: at
    `harness_max_loop_iterations == 1` the predicate was never consulted at all, so nothing was
    recorded and a capped turn reported no cap.

    `CappedModelCallLimit.before_model` sets `loop_capped` on the branch that stops the loop, so
    here the question is answered by reading the fact rather than by reasoning about a decision — or
    by comparing a count, which cannot distinguish "stopped at the cap" from "spent the last allowed
    call and then finished", because both end at exactly `cap`.

    Args:
        state: The state the finished run **returned**. Not `graph.get_state(config).values`:
            `loop_capped` is an untracked channel, so it is deliberately absent from the
            checkpoint a later read would restore, and asking there gets a silent `False`.

    Returns:
        Whether the run reached the configured iteration cap.
    """
    return bool(state.get("loop_capped", False))
