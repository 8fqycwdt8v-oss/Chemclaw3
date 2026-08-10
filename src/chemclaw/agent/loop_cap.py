"""Make the harness loop's runaway cap observable, so a capped turn stops looking finished.

`AgentLoopMiddleware` stops at `harness_max_loop_iterations` and **returns normally, emitting
nothing** — so a capped turn is externally identical to one that finished its work. That silence
cost twice. A deployment had no signal to alert on (`docs/planning/BACKLOG.md`), and
`chemclaw.evals.autonomy.runaway_rate` was reduced to inferring a runaway from *residue*: an answer
sent while the plan still held unchecked steps. Residue cannot tell "abandoned a step" from
"correctly deferred to a durable job", because `chemclaw.agent.harness_todo.mark_awaiting_job`
leaves exactly the same trace — an open todo — behind a turn that did the right thing.

**Where the signal comes from.** MAF offers no hook on the cap itself: `_evaluate_stop`
short-circuits `should_continue` once the cap is reached, and the middleware is constructed inside
`create_harness_agent` rather than handed in. What it does hand in is the loop predicate, which is
ours — and one fact about the loop is enough:

    the loop stopped at the cap exactly when its last stop decision was "keep going".

Every other way the loop ends is the predicate returning `False` (no todos left, the session is no
longer in execute mode, the plan is unapproved). Once it has said "keep going", the only thing that
can stop the loop without asking it again is the cap. So this module records each decision and the
runner reads the last one.

A cap of `1` makes the loop single-shot and MAF never consults the predicate at all, so nothing is
recorded and the turn reports no cap. That is the honest reading rather than a hole: a loop that
never got to want another iteration was not stopped from taking one.

The carrier is a contextvar holding a *mutable* record, for the reasons
`chemclaw.core.turn_signals` gives for its buffer: it is task-local (concurrent turns cannot see
each other's loops), it is empty off the request path (CLI, tests, the classic agent), and it is
mutated rather than rebound — so the decision is visible to the runner even when the agent's stream
is driven from a task of its own.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import before_model

from chemclaw.agent.harness_types import ShouldContinueCallable, ShouldContinueResult
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoopWatch:
    """One turn's last loop decision — `True` when the loop still wanted another iteration."""

    wants_more: bool = False


_watch: ContextVar[_LoopWatch | None] = ContextVar("chemclaw_loop_watch", default=None)


def begin_loop_watch() -> object:
    """Start watching this turn's loop decisions; returns a token for `end_loop_watch`."""
    return _watch.set(_LoopWatch())


def end_loop_watch(token: object) -> None:
    """Tear the turn's watch down (mirrors every other ambient's reset)."""
    _watch.reset(token)  # type: ignore[arg-type]


def loop_hit_cap() -> bool:
    """Whether the harness loop was stopped by its iteration cap during this turn.

    `False` off the request path and for every agent that does not loop, which is what makes this
    safe to ask unconditionally: no watch, no cap. See the module docstring for why "the last
    decision was keep going" is the same statement as "the cap fired".
    """
    watch = _watch.get()
    return watch is not None and watch.wants_more


def observe_loop_cap(
    inner: ShouldContinueCallable,
) -> Callable[..., Awaitable[ShouldContinueResult]]:
    """Wrap the loop predicate so the turn can tell a capped loop from a completed one.

    Wraps rather than replaces: the decision is `inner`'s alone, including the `(bool, str | None)`
    feedback MAF routes to `next_message` — dropping that string would silently disable the "these
    todos are still open" reminder (`chemclaw.agent.plan_gate.approved_todos_remaining` records the
    same reasoning). This only *reads* the answer on its way past.

    Applied outermost of the predicate chain, so what it records is the decision the loop acted on
    rather than one input to it — an unapproved plan stopping the loop is a deliberate stop, not a
    runaway.
    """

    async def _should_continue(**kwargs: Any) -> ShouldContinueResult:
        # Sync or async, per MAF's own predicate contract — normalized exactly as `plan_gate` does,
        # for the same reason: three lines beat importing an underscore-prefixed helper.
        raw = inner(**kwargs)
        decision = await raw if inspect.isawaitable(raw) else raw
        proceed = bool(decision[0]) if isinstance(decision, tuple) else bool(decision)
        watch = _watch.get()
        if watch is not None:
            watch.wants_more = proceed
        return decision

    return _should_continue


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
def lg_loop_cap(state: Mapping[str, Any], runtime: Any) -> dict[str, Any] | None:
    """Count this turn's model calls and end the run when it reaches the cap.

    **Why a counter here rather than `ModelCallLimitMiddleware`.** That middleware enforces exactly
    this, and using it was the first attempt. It keeps two counts — `thread_model_call_count`, which
    persists, and `run_model_call_count`, which does not — and the one that matches a *turn* is the
    run count. Measured against a checkpointed session, the final state carries the thread count and
    no run count at all, so "was this turn capped" was unanswerable from it. Enforcing with that
    middleware and counting again here would have meant two counters for one number; enforcing here
    means one number that is both the limit and the record.

    That is the whole point. MAF's cap fired inside `create_harness_agent` where nothing could
    observe it, so a capped turn was externally identical to a finished one and `loop_hit_cap` had
    to *infer* it — an inference blind at a cap of 1, because the loop never consults the predicate
    there. Here the count is a declared state field, so a cap of 1 leaves a count of 1.

    Ending the run rather than raising, matching MAF: the answer the last iteration managed still
    goes out, and a surface marks it partial (`chemclaw.api.runner` does this off `loop_hit_cap`).
    A raised error would discard work a chemist is entitled to see.
    """
    calls = int(state.get("model_calls", 0))
    if calls >= settings.harness_max_loop_iterations:
        logger.warning("the model loop hit its %d-iteration cap", calls)
        record_loop_cap()
        return {"jump_to": "end"}
    return {"model_calls": calls + 1}


def record_loop_cap() -> None:
    """Tell this turn's watch the cap fired, so **one** reader answers for both engines.

    Without this the count was kept where nothing on the turn path read it. `chemclaw.api.runner`
    decides whether to emit `loop_cap_reached` and increment `chemclaw_turn_loop_caps_total` by
    calling `loop_hit_cap()`, which reads the ambient watch — and only `observe_loop_cap`, the MAF
    half, ever wrote it. `loop_capped(state)` answers the same question from graph state and has no
    caller in the runner, because a compiled graph's final state is not something the streaming
    driver hands back.

    So a capped turn on the graph engine was externally identical to a finished one: no error
    event, no counter, nothing for a surface to mark the answer partial with. That is precisely the
    defect `lg_loop_cap` exists to fix — "MAF's cap fired inside `create_harness_agent` where
    nothing could observe it" — reintroduced one layer up by wiring the runner to the wrong reader.

    Marking the watch rather than branching in the runner is what keeps it one number: the count
    still lives in `model_calls` and `loop_capped` still reads it, and this records only the *fact*
    the runner asks about. A second branch there would be a second place for the two engines to
    disagree about whether a turn was cut off.
    """
    watch = _watch.get()
    if watch is not None:
        # `wants_more` means "the loop asked to continue and something else stopped it", which is
        # exactly what a cap is. Mutated rather than rebound for the reason the module docstring
        # gives: the runner must see it even when the stream is driven from a task of its own.
        watch.wants_more = True


def loop_capped(state: Mapping[str, Any]) -> bool:
    """Whether this turn's model loop was stopped by its cap — **read, not inferred**.

    The LangGraph counterpart of `loop_hit_cap`, and a different kind of answer. MAF offers no hook
    on its cap: `_evaluate_stop` short-circuits the predicate once the limit is reached, and the
    middleware is constructed inside `create_harness_agent` rather than handed in, so the only
    signal available was the shape of the *last decision the loop asked for* — "it wanted another
    iteration, and something other than the predicate stopped it". That inference is sound and it
    has a hole its own docstring records: at `harness_max_loop_iterations == 1` the loop never
    consults the predicate at all, so nothing is recorded and a capped turn reports no cap.

    `lg_loop_cap` keeps the count in a declared state field, so here the question is answered by
    reading the number rather than by reasoning about a decision. The hole closes with it: a cap of
    1 that fired leaves a count of 1, which is exactly what this compares.

    Args:
        state: The turn's final graph state.

    Returns:
        Whether the run reached the configured iteration cap.
    """
    return int(state.get("model_calls", 0)) >= settings.harness_max_loop_iterations
