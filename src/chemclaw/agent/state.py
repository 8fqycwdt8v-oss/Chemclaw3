"""The conversation graph's typed state, and the one function that starts a turn in it.

The framework layer 1 was first built on held its plan, its mode and its bookkeeping in a
`session.state` dict keyed by strings nobody declared. Two of this migration's findings came
straight out of that: the loop cap had to *infer* whether it had fired because nothing recorded it,
and a todo waiting on a durable job was marked by prefixing its `description` with `awaiting-job:` —
a convention that existed only because the item type had no field to put it in.

Here each of those is a named field with a declared type, which is what makes the rest of the
rebuild cheap rather than clever.

**Extends `PlanningState`, not `AgentState`.** `TodoListMiddleware` declares `todos` and the
`write_todos` tool that maintains them, so the plan itself is already typed by the middleware that
owns it; adding a second list beside it would give the graph two answers to "what is the plan".

**Every field here is either per-turn or per-thread, and `turn_input` is what makes that true.**
The checkpointer persists the whole state under `thread_id`, and `thread_id` is the *session* id —
so a field is per-thread by default and per-turn only if something resets it. Nothing did, and the
consequence was not theoretical: `model_calls` accumulated across turns, so the runaway cap fired on
the *session's* fourth model call rather than the turn's, and every later turn on that session ended
before the model was called at all. Measured at `harness_max_loop_iterations=3`: turns 0-2 answered,
turn 3 returned the user's own question. A session bricked with no way back.

Starting a turn therefore goes through `turn_input`, which names the per-turn fields and zeroes
them. A caller that hand-builds `{"messages": ...}` gets the old defect back, which is why there is
one function and `tests/test_langgraph_agent.py` asserts every invoke site uses it.
"""

from typing import Any

from langchain.agents.middleware.todo import PlanningState


class ChemclawState(PlanningState):
    """The graph state Chemclaw adds on top of the plan the todo middleware maintains.

    Fields arrive with the phase that reads one — a declared field nothing consults is the same
    stub as a function nothing calls, and reads as coverage while proving nothing.
    """

    # How many model calls *this turn* has made — the runaway guard's counter
    # (`agent/loop_cap.py`). A field rather than a framework internal because the whole defect being
    # fixed is that the previous engine's cap fired where nothing could observe it. Reset by
    # `turn_input`; see the module docstring for what it cost when nothing did.
    model_calls: int


def turn_input(message: str) -> dict[str, Any]:
    """The graph input that starts one turn: the user's message, plus every per-turn field zeroed.

    **The reset is the point, not the message.** Handing the graph `{"messages": [...]}` alone
    resumes the checkpointed thread *including* counters that are only meaningful within a turn, and
    the checkpointer's whole job is that nothing is forgotten between turns. Naming the per-turn
    fields in one place is what keeps "this counts the turn" true of a field the thread outlives.

    Args:
        message: The user's message for this turn.

    Returns:
        The mapping to pass to `ainvoke`/`astream`. Every caller that starts a turn uses this;
        a caller that builds the mapping itself reintroduces the defect the docstring above records.
    """
    return {"messages": [("user", message)], "model_calls": 0}
