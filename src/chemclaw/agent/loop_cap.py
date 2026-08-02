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
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agent_framework._harness._loop import ShouldContinueCallable, ShouldContinueResult


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
