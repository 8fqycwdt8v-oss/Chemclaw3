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
where the shape comes from — and, since M14, where the *count itself* comes from:
`agent/loop_cap.py` subclasses that middleware rather than counting again, and the one field left
here is the record `PrivateStateAttr` makes upstream unable to leave behind.
"""

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.todo import PlanningState
from langgraph.channels.untracked_value import UntrackedValue

from chemclaw.core.config import settings


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
    exists to fix. It is exactly why the count itself is *not* declared here any more: upstream's
    `run_model_call_count` carries `PrivateStateAttr`, so it is unreadable by the time anyone asks,
    and this field is the answer to that rather than a second counter beside it.
    """

    # Whether the runaway guard stopped this turn. The *count* belongs to
    # `ModelCallLimitMiddleware` (`run_model_call_count`, an `UntrackedValue` channel this
    # declaration was copied from); what upstream cannot leave behind is the fact, because that
    # counter is stripped from the run's output by `PrivateStateAttr` and never checkpointed.
    # `loop_cap.CappedModelCallLimit.before_model` writes this on the branch that fires.
    #
    # Untracked, because a session whose third turn hit the cap would otherwise report every later
    # turn as capped, marking complete answers partial forever. The cost is that
    # `get_state(config).values` does not carry it: the value lives only in what the run returns,
    # which is where every reader already looks.
    # How many model calls *this turn* has made — the runaway guard's counter
    # (`agent/loop_cap.py`). A field rather than a framework internal, and that survived an attempt
    # to delegate it: `ModelCallLimitMiddleware` counts in `after_model`, which any middleware
    # declaring `after_model` with a `jump_to` runs *before* and short-circuits — measured, the
    # challenge gate's revision jump skipped the increment and the cap let one extra model call
    # through per round. `before_model` cannot be skipped that way. See the module docstring.
    #
    # `UntrackedValue` is what makes "this turn" true of it: the channel is never written to a
    # checkpoint, so a new run of the graph on the same `thread_id` starts it empty and
    # `enforce_loop_cap`'s `state.get("model_calls", 0)` reads 0. It is also *not* private, which is
    # what lets one budget span a whole team turn: `SubAgentMiddleware` strips private keys in both
    # directions, so a private counter would give every specialist a fresh allowance.
    model_calls: NotRequired[Annotated[int, UntrackedValue]]

    loop_capped: NotRequired[Annotated[bool, UntrackedValue]]

    # How many revision rounds the challenge panel has already forced this turn. Bounds the
    # `jump_to: "model"` loop in `agent/challenge_gate.py` against `challenge_max_attempts`, and it
    # is a counted field for exactly the reason the runaway cap's own record is: the alternative is
    # inferring "have we been round this before" from the message list, which is the inference
    # `agent/loop_cap.py` was written to delete.
    #
    # **This field was deleted once, by a merge, and nothing went red.** `challenge_gate` writes it
    # and LangGraph silently drops a write to an undeclared channel, so `attempts` stayed 0, the
    # `challenge_max_attempts` bound never fired, and the revision loop ran to the recursion limit —
    # discarding the whole turn. `tests/test_state_channels.py` now drives a compiled graph for
    # every channel here, because a unit test on the hook cannot see a channel that does not exist.
    challenge_attempts: NotRequired[Annotated[int, UntrackedValue]]


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
    """The invocation config one turn runs under: its thread, and the graph's step ceiling.

    **The ceiling is the point.** `create_agent` bakes `recursion_limit=9999` and nothing in this
    repo had ever chosen otherwise, so the only bound on a turn was thousands of model calls —
    measured at 2 supersteps per call on the classic path and 4 with the harness, i.e. roughly 5,000
    and 2,500. Worse, reaching it raises `GraphRecursionError`, which discards whatever the turn had
    produced; `agent.loop_cap` states the opposite position explicitly, that a chemist is entitled
    to see the work the last iteration managed. The cap is the graceful stop and this is the
    backstop under it — and on the classic path, where the loop cap is not attached at all, it
    is the only bound there is.

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
    if thread_id is not None:
        config["configurable"] = {"thread_id": thread_id}
    return config
