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
`model_calls` and `loop_capped` are untracked channels (`chemclaw.agent.state`'s `TurnTotal` and
`TurnFlag`), so the checkpointer never persists them and every run of the graph starts the count at
0 — including a run on a `thread_id` a previous turn already used. Nothing here has to be reset, and
nothing here can be forgotten. Those two classes exist because the same fields cross the subagent
boundary (regression 3 below), which is what puts two writes for one key in a single superstep.

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
from langchain_core.messages import AIMessage, HumanMessage

from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _LoopWatch:
    """One turn's cap mark and its live call count, shared by every branch of the turn.

    `capped` is `True` once the loop was stopped by its iteration cap. `calls` is what the state
    channel cannot be: a number every concurrent branch of one turn reads and advances.

    **Why the count has to live here as well as in the channel.** `model_calls` is a `TurnTotal`,
    and
    `SubAgentMiddleware` hands every helper in a `task` batch the *same pre-superstep* value — so
    each
    of `W` branches compared the cap against its own private copy of that base and each
    independently
    spent the whole remaining allowance. The channel then folded them additively, which makes the
    recorded count right and the *bound* wrong: the parent only learns the total once every branch
    has
    finished spending it. Measured at a cap of 4 over 8 helpers: **25 model calls**, following
    `1 + W·(cap − 1)`; at shipped defaults (cap 25, `agent_max_parallel_tool_calls` 8) that is 193
    calls in one turn. CLAUDE.md's "counted in a `TurnTotal` channel so a fan-out shares one budget"
    was true of the counting and false of the sharing.

    This is the same shape `agent/spend_cap.py` already relies on for the cost half — `TurnUsage` is
    one mutable object every branch books into, which is why the spend cap was only partly exposed
    where this one was fully exposed — and the same shape `tool_result_size.batch_siblings` uses for
    the file budget (`D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep`). A contextvar
    is *copied* into each branch's task, but the object it points at is not, so a mutation is
    visible
    to every sibling; that is the property `record_loop_cap` already depends on and says so.

    The floor is only as wide as the watch: off the request path there is no watch, and the cap
    falls
    back to the channel and the thread, which is the pre-existing behaviour rather than a new hole.
    `docs/planning/BACKLOG.md` carries the row for the two paths that do not open one.
    """

    capped: bool = False
    calls: int = 0


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
    """Authorise this turn's next model call, or end the run because the cap is reached.

    **The increment counts authorisations rather than completions**, and it cannot do otherwise
    from here: `before_model` is the one hook no later middleware can skip, which is the whole
    reason the counter is first-party (see below), and it necessarily runs before anything knows
    whether the call happens. A later `before_model` hook that jumps — `spend_cap.enforce_spend_cap`
    is one, ordered immediately after this — leaves the increment for a call that was never made.
    `ChemclawState.model_calls` says so; moving the count to where the call is made would mean a
    second hook writing the number this one enforces on, which is the property this module exists
    to keep.

    **Why a counter here rather than `ModelCallLimitMiddleware`.** That middleware enforces exactly
    this, and delegating to it has now been tried twice. The first reason was observability: it
    keeps
    `thread_model_call_count` (persisted) and `run_model_call_count` (not), and both carry
    `PrivateStateAttr`, so neither is readable from what a finished run returns — "was this turn
    capped" was unanswerable from it.

    **The second attempt subclassed it to add that record, shipped, and was reverted for four
    regressions no amount of recording fixes.** All four come from where upstream increments, or
    from what its channels are:

    1. **The increment is skippable.** Upstream counts in `after_model`. `after_model` hooks run in
       reverse list order, so `challenge_gate.challenge_answer` — `@after_model(can_jump_to=[...])`
       —
       runs first, and its `jump_to: "model"` short-circuits the rest of the chain including the
       increment. Measured at cap 2: 2, 3, 4, 5 model calls for 0, 1, 2, 3 revision rounds, with
       `run_model_call_count` observed as `[0, 0, 0]` across three real calls. `before_model` runs
       before the model regardless of what any later hook decides, so it cannot be skipped.
    2. **`exit_behavior="end"` fabricates an assistant message.** Upstream returns
       `{"jump_to": "end", "messages": [AIMessage("Model call limits exceeded: ...")]}`.
       `cli/chat.py`
       prints `messages[-1].content`, so a capped CLI turn printed the limit string *instead of* the
       partial answer — the exact outcome this module's own docstring rejects. `SubAgentMiddleware`
       builds a specialist's report from the last non-empty `AIMessage`, so a capped specialist
       reported the limit string and dropped its work. And `messages` is checkpointed, so the
       fabricated turn is replayed into the model's context forever. This hook emits no message.
    3. **The team's budget silently became per-specialist.** `model_calls` is untracked and
       *not* private, so it crosses into and out of a subagent and one budget spans the team.
       Upstream's counter is `PrivateStateAttr`, which `SubAgentMiddleware` strips both ways, so
       every specialist started at 0 — a five-specialist turn's ceiling went from ~N to ~6N.
    4. **`thread_model_call_count` is a `LastValue`**, i.e. checkpointed. Subclassing put a
       monotonically growing int nothing reads, resets or prunes into every session's checkpoint,
       and shrank the first-party channel stamp — which makes an old pod refuse a thread a new pod
       has written, during a rolling deploy.

    The general finding, recorded so the third attempt does not have to rediscover it:
    **`ModelCallLimitMiddleware` is unsafe to compose with any middleware that jumps from
    `after_model`.** Reconsider only if upstream moves the increment to `before_model` or exposes a
    non-message exit.

    That is the whole point. The cap this replaced fired inside the framework's own loop where
    nothing could observe it, so a capped turn was externally identical to a finished one and
    `loop_hit_cap` had to *infer* it — an inference blind at a cap of 1, because the loop never
    consulted the predicate there. Here the count is a declared state field, so a cap of 1 leaves
    a count of 1.

    Ending the run rather than raising: the answer the last iteration managed still goes out, and
    a surface marks it partial (`chemclaw.api.runner` does this off `loop_hit_cap`). A raised error
    would discard work a chemist is entitled to see.
    """
    # **The channel is `UntrackedValue`, so a resumed turn reads 0 here and gets a fresh cap.**
    # `agent/state.py` says the channel "starts empty on every run of the graph", which was the
    # per-turn guarantee for as long as one turn was one run. A turn can now be resumed after a pod
    # death (`D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up`), and
    # measured, a turn that dies *n* times got *n+1* full allowances.
    #
    # So the thread itself is the floor: this turn's assistant messages since the last human one.
    # The same `max` shape `enforce_spend_cap` already uses against `metered_turn_tokens`, for the
    # same *shape* — but not for the same reason, and the sentence claiming it did was wrong in
    # the reassuring direction. `enforce_spend_cap` reads `metered_turn_tokens()`, which is a
    # **contextvar** defaulting to `None`, and `billed_tokens` is an untracked `TurnTotal`: in a
    # resuming process both are 0, so the billed-token budget still resets on every resume while
    # this counter no longer does. Only the model-call half of that parity exists.
    #
    # On an ordinary turn the floor changes nothing, and again not for the reason first written
    # here: the increment writes `calls + 1` but the comparison runs before the write, so the two
    # are equal on every call rather than the channel leading by one. Measured at a cap of 4:
    # 0/0, 1/1, 2/2, 3/3, 4/4.
    # **The turn-wide count is the third floor, and it is the only one a sibling branch can move.**
    # The two below are this branch's own: the channel is the pre-superstep snapshot every helper in
    # a
    # `task` batch was handed, and the thread is this branch's messages. See `_LoopWatch` for the
    # measurement — without this term a fan-out of width `W` spends `W` allowances.
    watch = _watch.get()
    # **Two numbers, and folding them into one inflates the channel.** `own` is this branch's own
    # count and is what the channel advances to, so `TurnTotal`'s `max(value - base, 0)` still folds
    # to one advance per real call. `turn` is what the *cap* compares against. Writing `turn + 1` to
    # the channel instead would have every sibling advance past every other sibling's advance and
    # report a fan-out of 2 calls as 3.
    own = max(int(state.get("model_calls", 0)), calls_already_made(state.get("messages")))
    turn = max(own, watch.calls if watch is not None else 0)
    calls = turn
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
    # Advanced here rather than where the channel is written, because this is the hook that
    # *authorises* the call — and mutated rather than rebound, so every branch sharing this object
    # sees it. `max` rather than `+= 1`: two branches that both read `base` must not each add one to
    # a number the other has already advanced past, and an absolute write is what `TurnTotal`'s own
    # docstring says a delta cannot be.
    if watch is not None:
        watch.calls = turn + 1
    return {"model_calls": own + 1}


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
    return bool(state.get("loop_capped", False))


def calls_already_made(messages: Any) -> int:
    """How many model calls this turn has already made, read off the thread itself.

    **The caps are `UntrackedValue` on purpose and that is not a defect to undo.** `agent/state.py`
    says what the channel guarantees — it "starts empty on every run of the graph because there is
    nothing for the checkpoint to restore" — and per-turn-ness comes from exactly that. The design
    assumed one turn is one run, which was true until a turn could be resumed: measured, a turn that
    dies *n* times gets *n+1* fresh `harness_max_loop_iterations` and
    `agent_max_turn_billed_tokens` allowances.

    So the count is re-derived rather than persisted, from state that already survives a pod death.
    A turn's model calls are its assistant messages since the last human one — the whole thread's
    count would bound the *conversation* rather than the turn, which is a different and much
    tighter control than the one intended.

    It is read as a floor rather than a replacement (`enforce_loop_cap` takes the `max`), and on an
    ordinary turn it changes nothing. **The reason is not the one written here first.** That said
    the channel "always leads this by one and wins the `max`", which sounded like a safety margin
    and is not: the increment writes `calls + 1` but the comparison happens *before* the write, so
    at the comparison point the two are equal. Instrumented over a real default-profile turn at a
    cap of 4: `channel=0 floor=0`, `1/1`, `2/2`, `3/3`, `4/4` — a tie on every call, never a lead.
    So the `max` is a floor and nothing more, and if this function ever over-counted by one, healthy
    turns would cap an iteration early rather than being absorbed by a margin. On a resume the
    channel is 0 and this is the answer.

    **It cannot recover the call that was in flight when the pod died**, because that call produced
    no message — so a resumed turn is still permitted one more call than it should be. One, once
    per death, against a cap of 25; stated rather than papered over, and the alternative is a
    durable per-call write on the hot path.

    Args:
        messages: The thread, oldest first, as the checkpoint holds it.

    Returns:
        Assistant messages since the last human message, or over the whole list when there is none.
    """
    history = list(messages or [])
    start = 0
    for index in range(len(history) - 1, -1, -1):
        if isinstance(history[index], HumanMessage):
            start = index
            break
    return sum(1 for message in history[start:] if isinstance(message, AIMessage))
