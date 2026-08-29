"""The conversation graph's typed state, and the one function that starts a turn in it.

The framework layer 1 was first built on held its plan, its mode and its bookkeeping in a
`session.state` dict keyed by strings nobody declared. Two of this migration's findings came
straight out of that: the loop cap had to *infer* whether it had fired because nothing recorded it,
and a todo waiting on a durable job was marked by prefixing its `description` with `awaiting-job:` —
a convention that existed only because the item type had no field to put it in.

The first is a named field with a declared type here, which is what makes the rest of the rebuild
cheap rather than clever. The second turned out not to need one at all — see below.

**Extends `PlanningState`, not `AgentState`.** `TodoListMiddleware` declares `todos` and the
`write_todos` tool that maintains them, so the plan itself is already typed by the middleware that
owns it; adding a second list beside it would give the graph two answers to "what is the plan".

**The marker convention is gone and nothing replaced it here**, which is the whole fix. The gate
must not count "a job this plan agreed to is now in flight" as a change to the plan — an approved
plan that revoked its own approval the first time it started a job would be unusable — and under
MAF that took a filter, because the bookkeeping lived in the same list as the plan. Now it does not
live there at all: a launched job is a `job_records` row and a `session_events` push-back, so
`todos` holds the plan and only the plan, and the exclusion the gate needs is structural rather
than a parse.

An `awaiting_jobs: list[str]` field was declared here for that job before the durable side was
built, and the durable side went to the two stores above instead. Nothing ever wrote it or read it,
while three docstrings — this one, `plan_gate.enforce_plan_approval`'s and a test's — described it
as the mechanism. It is removed rather than filled in, by the rule immediately below: a declared
field nothing consults reads as coverage while proving nothing, and prose about it reads as a
design somebody can rely on.

**Every field here is either per-turn or per-thread, and the *channel* is what makes that true.**
The checkpointer persists the whole state under `thread_id`, and `thread_id` is the *session* id —
so a plain field resolves to a `LastValue` channel, which is checkpointed, which makes it per-thread
whatever its docstring says. Nothing reset the runaway guard's fields and the consequence was not
theoretical: the model-call count accumulated across turns, so the cap fired on the *session's*
fourth model call rather than the turn's, and every later turn on that session ended before the
model was called at all. Measured at `harness_max_loop_iterations=3`: turns 0-2 answered, turn 3
returned the user's own question. A session bricked with no way back.

That defect was first closed by zeroing the fields by hand in `turn_input`, which worked and was
the wrong shape: it made "per-turn" a property of every *call site* rather than of the field, so a
caller that hand-built `{"messages": ...}` — and `graph.ainvoke` accepts one — silently got the
bricked session back. The field below is an `UntrackedValue` channel, which LangGraph never
checkpoints (`checkpoint()` returns `MISSING`), so it starts empty on every run of the graph
because there is nothing for the checkpoint to restore. The invariant moved out of a convention and
into the schema, and there is no longer a way to spell the mistake.

`ModelCallLimitMiddleware` upstream declares its own per-run counter exactly this way
(`run_model_call_count: NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]`), which is
where the shape comes from — and the shape is *all* that was taken. M14 briefly delegated the count
itself to that middleware; `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`
reverted it, so both fields below are first-party and `agent/loop_cap.py` subclasses nothing.

**This paragraph is why the reversion is spelled out rather than merely undone in code.** For a day
the delegated design survived here in prose after the code went back: the two comments on the
fields below disagreed with each other, one describing a subclass that no longer existed and the
other describing the counter that had returned. A reader had no way to tell which half was current.

**Both fields are `UntrackedValue` *subclasses* rather than the class itself, and the reason is a
fan-out.** The two channels cross the subagent boundary on purpose, so a superstep with more than
one `task` call in it delivers more than one value for each of them — which bare `UntrackedValue`
refuses with `InvalidUpdateError`, killing the turn after every helper has already spent its
tokens. The classes below say what a concurrent write *means* for each field instead of refusing
it; see their own docstrings for why `guard=False`, the escape hatch the error message names, is
the wrong answer to it.
"""

from collections.abc import Sequence
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.todo import PlanningState
from langgraph.channels.untracked_value import UntrackedValue

from chemclaw.core.config import settings


class TurnTotal(UntrackedValue[int]):
    """An untracked counter that **folds** a superstep's writes instead of refusing them.

    `UntrackedValue` raises `InvalidUpdateError` when one superstep delivers two values for its
    key, and here that is a shipped failure rather than a hypothetical. `SubAgentMiddleware`'s
    `task` tool returns each helper's whole final state as a `Command` update, and `model_calls` is
    deliberately neither excluded nor private so that one budget spans the team — so two `task`
    calls in one assistant message, which the helper's own description invites ("or several at
    once"), delivered two values into one superstep and lost the whole turn. Reproduced on the
    graph `build_langgraph_agent` compiles; `tests/test_subagents.py` drives it.

    **`guard=False` is the escape hatch the error message names, and it is the wrong one.** It
    keeps whichever value arrived last, so every other branch's spend is discarded and a fan-out
    silently under-counts the shared budget — which is per-specialist allowances again, regression
    3 in `agent/loop_cap.py`'s list of reasons this counter is first-party at all.

    So the fold is **additive over each writer's own advance**, which is exact rather than merely
    defined. Every branch of a superstep was handed the same value when it began —
    `SubAgentMiddleware` builds each helper's input from the parent's state — so `value - base` is
    what that branch spent, and the sum of the advances is what the team spent. Measured on the
    real graph at a fan-out of two: 4 model calls made, 4 counted. `max` counts 3.

    One writer is the degenerate case and is unchanged: `base + (value - base) == value`, exactly
    what `UntrackedValue` would have stored. A branch reporting *fewer* calls than it was handed
    contributes 0 rather than a negative advance — this count is what a cap is compared against,
    and a write must not be able to walk it back.
    """

    def update(self, values: Sequence[int]) -> bool:
        """Store the base plus every writer's advance on it."""
        if not values:
            return False
        # `is_available()` rather than a comparison against `MISSING`: that sentinel lives in
        # langgraph's `_internal` package, and this is the public question with the same answer.
        base = int(self.value) if self.is_available() else 0
        self.value = base + sum(max(int(value) - base, 0) for value in values)
        return True


class TurnFlag(UntrackedValue[bool]):
    """An untracked flag that stays set once any writer in the turn has set it.

    The same `InvalidUpdateError` reaches `loop_capped`, for the same reason: it crosses the
    subagent boundary beside `model_calls`. Here last-writer-wins would be worse than a miscount —
    of two helpers finishing in one superstep, the uncapped one's `False` could overwrite the
    capped one's `True` and a truncated turn would be reported as complete, which is the defect
    `agent/loop_cap.py` exists to fix, arriving through the channel instead of through an
    inference.

    So the fold is `or`, and it also folds in the value already stored: a cap that fired is a fact
    about the turn, and nothing that did not hit one may unwrite it.
    """

    def update(self, values: Sequence[bool]) -> bool:
        """Set the flag if anything in this superstep set it, and never clear it."""
        if not values:
            return False
        self.value = any(values) or (self.is_available() and bool(self.value))
        return True


class ChemclawState(PlanningState):
    """The graph state Chemclaw adds on top of the plan the todo middleware maintains.

    Fields arrive with the phase that reads one — a declared field nothing consults is the same
    stub as a function nothing calls, and reads as coverage while proving nothing.

    **The field does not carry `PrivateStateAttr`**, and that is deliberate rather than an omission
    from the upstream declaration it otherwise copies. `PrivateStateAttr` is
    `OmitFromSchema(input=True, output=True)`, so it would strip the field from what `ainvoke`
    *returns* — and once the value is out of the checkpoint, the return is the only place left to
    read it. `loop_cap.loop_capped(state)` is that reader: it takes "the turn's final graph state",
    which callers get from `ainvoke`, and hiding the field from the output would leave it with
    nothing to read — a capped turn unreportable again, which is the defect `agent/loop_cap.py`
    exists to fix. Upstream's own `run_model_call_count` does carry `PrivateStateAttr` and is
    therefore unreadable by the time anyone asks, which is why neither field below delegates to it.
    """

    # How many model calls *this turn* has made — the runaway guard's counter
    # (`agent/loop_cap.py`). A field rather than a framework internal, and that survived an attempt
    # to delegate it: `ModelCallLimitMiddleware` counts in `after_model`, which any middleware
    # declaring `after_model` with a `jump_to` runs *before* and short-circuits — measured, the
    # challenge gate's revision jump skipped the increment and the cap let one extra model call
    # through per round. `before_model` cannot be skipped that way. See the module docstring.
    #
    # Untracked is what makes "this turn" true of it: the channel is never written to a
    # checkpoint, so a new run of the graph on the same `thread_id` starts it empty and
    # `enforce_loop_cap`'s `state.get("model_calls", 0)` reads 0. It is also *not* private, which is
    # what lets one budget span a whole team turn: `SubAgentMiddleware` strips private keys in both
    # directions, so a private counter would give every specialist a fresh allowance. That second
    # property is exactly what puts two writes in one superstep, which is `TurnTotal`'s subject.
    model_calls: NotRequired[Annotated[int, TurnTotal(int)]]

    # Whether the runaway guard stopped this turn — the *fact*, beside the count above. Both are
    # first-party: `loop_cap.enforce_loop_cap` reads the count in `before_model` and writes this on
    # the branch that fires, in the same hook, so the two cannot disagree about whether a cap was
    # reached. The untracked shape is copied from upstream's `run_model_call_count`; the counting
    # is not (see the module docstring).
    #
    # Untracked, because a session whose third turn hit the cap would otherwise report every later
    # turn as capped, marking complete answers partial forever. The cost is that
    # `get_state(config).values` does not carry it: the value lives only in what the run returns,
    # which is where every reader already looks.
    loop_capped: NotRequired[Annotated[bool, TurnFlag(bool)]]

    # What this turn has **billed** so far, across every model call it has made — the spend guard's
    # counter (`agent/spend_cap.py`), the cost-denominated sibling of `model_calls` above.
    #
    # The same three properties, for the same three reasons, and none of them is incidental:
    # untracked so the count is the *turn's* rather than the session's; not private so one budget
    # spans a turn that delegates, which is regression 3 in `agent/loop_cap.py`'s list; and
    # `TurnTotal` so a fan-out's concurrent writes fold additively instead of raising
    # `InvalidUpdateError` or silently keeping only the last branch's spend.
    #
    # Written from `wrap_model_call` rather than `after_model`, because only the response carries
    # the bill and an `after_model` write is skippable by any middleware that jumps from there
    # (`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`). The write is an
    # **absolute** total rather than a delta, which is what `TurnTotal`'s fold is defined against:
    # it stores `base + (value - base)`, so a delta would be read as a walk backwards and
    # contribute 0.
    billed_tokens: NotRequired[Annotated[int, TurnTotal(int)]]

    # Whether the spend guard stopped this turn — the fact beside the count, exactly as
    # `loop_capped` sits beside `model_calls`, and written on the one branch that stops the loop so
    # the two cannot disagree. A comparison on the count could not answer it: the stopping branch
    # does not bill, so a capped turn and a turn that spent its last allowed token and then
    # finished both end at the same number.
    spend_capped: NotRequired[Annotated[bool, TurnFlag(bool)]]


def turn_input(message: str) -> dict[str, Any]:
    """The graph input that starts one turn: the user's message.

    **This is no longer where per-turn-ness comes from** — the two fields above are untracked
    channels, so they reset because the checkpoint cannot restore them, not because a caller
    remembered to zero them. What is left here is the one-line shape of a turn's input, kept as a
    function for two reasons rather than inlined at its four call sites: it is the seam a turn's
    invocation shape belongs to (a `recursion_limit` config sibling is the next thing to land beside
    it), and it keeps `("user", message)` — the tuple form the graph coerces — written once.

    Args:
        message: The user's message for this turn.

    Returns:
        The mapping to pass to `ainvoke`/`astream`.
    """
    return {"messages": [("user", message)]}


def turn_config(thread_id: str | None = None) -> dict[str, Any]:
    """The invocation config one turn runs under: its thread, step ceiling, and fan-out bound.

    **The ceiling is the point.** `create_agent` bakes `recursion_limit=9999`, `create_deep_agent`
    bakes a second one onto the graph it returns, and nothing in this
    repo had ever chosen otherwise, so the only bound on a turn was thousands of model calls —
    measured at 2 supersteps per call on the classic path and 4 with the harness, i.e. roughly 5,000
    and 2,500. Worse, reaching it raises `GraphRecursionError`, which discards whatever the turn had
    produced; `agent.loop_cap` states the opposite position explicitly, that a chemist is entitled
    to see the work the last iteration managed. The cap is the graceful stop — attached on every
    profile since the harness gate on it expired with the second engine — and this is the backstop
    under it, sized so the cap always fires first.

    One function so the number is chosen once. `turn_input` is its sibling on the input side; the
    per-turn *state* reset that used to live there is now the channel's job (see `ChemclawState`),
    which is why this is a config and not a second input builder.

    Args:
        thread_id: The checkpointed session to continue, or `None` for a graph built without a
            checkpointer — a template step, which is one bounded turn with no thread at all.

    Returns:
        The config to pass to `ainvoke`/`astream`.
    """
    config: dict[str, Any] = {"recursion_limit": settings.agent_recursion_limit}
    # The fan-out bound beside the step ceiling: `ToolNode` gathers a whole parallel batch with no
    # limit of its own, so this is the one knob that keeps a 40-call assistant message from taking
    # 40 pool connections at once. LangGraph reads `max_concurrency` per superstep; 0 means a
    # deployment chose unbounded, spelled by omission because the key's absence *is* upstream's
    # unbounded default.
    if settings.agent_max_parallel_tool_calls:
        config["max_concurrency"] = settings.agent_max_parallel_tool_calls
    if thread_id is not None:
        config["configurable"] = {"thread_id": thread_id}
    return config


def answer_text(result: Any) -> str:
    """The final assistant text out of a completed graph turn — the output side of `turn_input`.

    The graph returns its whole message list rather than a single `response.text`, so the answer is
    the last message's content. Joined across content blocks because a model may answer in parts,
    and coerced with `str` so a caller never fails on a shape the model managed to produce.

    **One definition, because there were two.** `cli/chat.py` and `durable/template_activities.py`
    each carried a byte-identical copy — the only exact structural clone in the tree — so the
    reasoning above lived beside one of them and the other had a one-line docstring. Both already
    import this module for `turn_input`/`turn_config`, which is why the shared home is here and not
    a new one: this is the third function about the shape of a turn.
    """
    messages = result.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)
