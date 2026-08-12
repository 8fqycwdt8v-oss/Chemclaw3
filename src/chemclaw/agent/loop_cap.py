"""Make the model loop's runaway cap observable, so a capped turn stops looking finished.

A loop that stops at its iteration cap and **returns normally, emitting nothing** is externally
identical to one that finished its work. That silence cost twice under the framework this layer was
first built on. A deployment had no signal to alert on (`docs/planning/BACKLOG.md`), and
`chemclaw.evals.autonomy.runaway_rate` was reduced to inferring a runaway from *residue*: an answer
sent while the plan still held unchecked steps. Residue cannot tell "abandoned a step" from
"correctly deferred to a durable job", because a turn that defers correctly leaves exactly the same
trace — an open todo — behind.

**The cap is now a counted state field, not an inference.** `enforce_loop_cap` is a `before_model`
hook over `ChemclawState.model_calls`: it counts each model call, and when the count reaches
`harness_max_loop_iterations` it jumps the graph to `end` and marks the turn. So the number that
enforces the limit and the number that records it are the same number, and there is nothing to
reason about. What this replaced was an inference — "the loop stopped at the cap exactly when its
last stop decision was keep going" — which was sound and had a hole at a cap of 1, where the
predicate was never consulted at all and a capped turn reported no cap.

**The count is per *turn*, and that is a property of the channel rather than of the caller.**
`model_calls` and `loop_capped` are declared `UntrackedValue` in `chemclaw.agent.state`, so the
checkpointer never persists them and every run of the graph starts the count at 0 — including a run
on a `thread_id` a previous turn already used. Nothing here has to be reset, and nothing here can
be forgotten.

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
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import before_model

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


# `can_jump_to` is not decoration, it is the edge. **Without it the cap was inert**, and inert in
# the worst way: the hook ran, counted correctly, decided correctly and returned `{"jump_to":
# "end"}` on every call after the limit — and the graph went on looping, because `before_model`'s
# conditional edge is *built from this declaration*. No declaration, no edge, so nothing reads the
# instruction. Measured at a cap of 1: the hook fired five times and said "end" four times while
# four further model/tool round-trips completed anyway.
#
# That is why the unit test passed and the turn did not. Calling the hook proves the decision; only
# a compiled graph proves the decision is connected to anything. This is the same shape as the
# `to_regclass` guard M6 nearly shipped — a check that runs, returns the right answer, and is wired
# to nothing.
@before_model(can_jump_to=["end"])
def enforce_loop_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """Count this turn's model calls and end the run when it reaches the cap.

    **Why a counter here rather than `ModelCallLimitMiddleware`.** That middleware enforces exactly
    this, and using it was the first attempt. It keeps two counts — `thread_model_call_count`, which
    persists, and `run_model_call_count`, which does not — and the one that matches a *turn* is the
    run count. Measured against a checkpointed session, the final state carries the thread count and
    no run count at all, so "was this turn capped" was unanswerable from it. Enforcing with that
    middleware and counting again here would have meant two counters for one number; enforcing here
    means one number that is both the limit and the record.

    That is the whole point. The cap this replaced fired inside the framework's own loop where
    nothing could observe it, so a capped turn was externally identical to a finished one and
    `loop_hit_cap` had to *infer* it — an inference blind at a cap of 1, because the loop never
    consulted the predicate there. Here the count is a declared state field, so a cap of 1 leaves
    a count of 1.

    Ending the run rather than raising: the answer the last iteration managed still goes out, and
    a surface marks it partial (`chemclaw.api.runner` does this off `loop_hit_cap`). A raised error
    would discard work a chemist is entitled to see.
    """
    calls = int(state.get("model_calls", 0))
    if calls >= settings.harness_max_loop_iterations:
        logger.warning("the model loop hit its %d-iteration cap", calls)
        record_loop_cap()
        # `loop_capped` is written here and nowhere else, because **the count cannot answer the
        # question**. This branch stops the loop without incrementing, so a capped turn and a turn
        # that used its last allowed call and then finished normally both end at exactly `cap` —
        # measured at a cap of 1, where a one-call turn that answered was reported as capped and its
        # complete answer was marked partial. A comparison on the count is a guess either way round;
        # a flag set by the branch that fires is the fact.
        return {"jump_to": "end", "loop_capped": True}
    return {"model_calls": calls + 1}


def record_loop_cap() -> None:
    """Mark this turn's watch, so the runner can see a cap it cannot read off the state.

    `chemclaw.api.runner` decides whether to emit `loop_cap_reached` and increment
    `chemclaw_turn_loop_caps_total` by calling `loop_hit_cap()`. It has no other way to ask: a
    compiled graph's final state is not something the streaming driver is handed back, so
    `loop_capped(state)` — the authoritative reader — is unreachable from there.

    Without this mark a capped turn was externally identical to a finished one: no error event, no
    counter, nothing for a surface to mark the answer partial with. That is the very defect
    `enforce_loop_cap` exists to fix, reintroduced one layer up by leaving the runner with no
    reader at all.

    Marking rather than branching in the runner is what keeps it one number: the count still lives
    in `model_calls` and `loop_capped` still reads it, and this records only the *fact* the runner
    asks about.
    """
    watch = _watch.get()
    if watch is not None:
        # Mutated rather than rebound, for the reason the module docstring gives: the runner must
        # see it even when the stream is driven from a task of its own.
        watch.capped = True


def loop_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn's model loop was stopped by its cap — **read, not inferred**.

    The authoritative answer, and a different kind of answer from `loop_hit_cap`. The framework
    this layer was first built on offered no hook on its cap — it short-circuited the loop
    predicate once the limit was reached — so the only signal available was the shape of the *last
    decision the loop asked for*: "it wanted another iteration, and something other than the
    predicate stopped it". That inference was sound and had a hole: at
    `harness_max_loop_iterations == 1` the predicate was never consulted at all, so nothing was
    recorded and a capped turn reported no cap.

    `enforce_loop_cap` sets `loop_capped` on the branch that stops the loop, so here the question is
    answered by reading the fact rather than by reasoning about a decision — or, as an earlier
    version did, by comparing the count, which cannot distinguish the two cases: the stopping branch
    does not increment, so a capped turn and a turn that spent its last allowed call and then
    finished both end at exactly `cap`.

    Args:
        state: The state the finished run **returned**. Not `graph.get_state(config).values`:
            `loop_capped` is an untracked channel, so it is deliberately absent from the
            checkpoint a later read would restore, and asking there gets a silent `False`.

    Returns:
        Whether the run reached the configured iteration cap.
    """
    # `>`, not `>=`. `enforce_loop_cap` increments *before* the model call and jumps to `end` when
    # the count has already reached the cap — so a turn that used its last allowed call and then
    # finished normally leaves `model_calls == cap` without ever having been stopped. Reading that
    # as "capped" marks a complete answer partial, which is the opposite of this function's job.
    return bool(state.get("loop_capped", False))
