"""The iteration cap's durable floor: what a turn has already spent, read off the thread.

`D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up` measured that the
caps are `UntrackedValue` and a resume is a new run of the graph, so a turn that dies *n* times
gets *n+1* full `harness_max_loop_iterations` allowances. `calls_already_made` is the answer: the
count is re-derived from state that already survives a pod death rather than persisted on the hot
path.

**This file is what is left of `tests/test_resume.py`, and the deletion is the finding.**
`agent/resume.py`'s public surface — `resume_turn`, `resumability`, `Resumability`,
`ResumeOutcome` — had no caller anywhere in `src/`, only this test file, which is the shape
`CLAUDE.md` names by hand: "a guard with no caller, kept alive by a test that calls it directly, is
the `map_to_hpc_identity` shape — a claim that a control exists". It also shipped three defects a
caller would have hit, recorded in
`D-2026-09-15-a-resume-with-no-caller-is-three-untested-defects`: `resume_turn` established none of
the four per-turn ambients `api/runner.py` sets, and `plan_gate.enforce_plan_approval` returns
early when `get_current_session_id()` is empty — so every side-effecting tool replayed on a resume
skipped the plan gate; the lease was taken once and never refreshed against a 60 s default, so any
resume longer than a minute reopened the fork the module existed to prevent; and the
`__pregel_tasks` read its own ADR calls a requirement was never written.

`calls_already_made` is kept because it has a real consumer — `enforce_loop_cap` — and closes a
real hole on its own.
"""

from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chemclaw.agent.loop_cap import calls_already_made, enforce_loop_cap
from chemclaw.core.config import settings


def test_a_resumed_turn_does_not_get_a_fresh_iteration_budget() -> None:
    """The caps are `UntrackedValue` by design, and a resume is a new run of the graph.

    `agent/state.py` states the channel's guarantee — it "starts empty on every run of the graph
    because there is nothing for the checkpoint to restore" — and per-turn-ness came from exactly
    that, which held for as long as one turn was one run.

    Four arms, because each alone would pass for the wrong reason:

    - a resumed turn counts the calls it already made;
    - the count is *this turn's*, not the conversation's — the whole-thread number would bound a
      long chat far tighter than intended, which is a different control wearing this one's name;
    - a thread with no human message at all still answers rather than raising;
    - an empty or absent thread is zero rather than an exception.
    """
    killed_mid_turn = [
        HumanMessage("an earlier question"),
        AIMessage("an earlier answer"),
        HumanMessage("this turn"),
        AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "1"}]),
        ToolMessage("a result", tool_call_id="1"),
        AIMessage("partial work"),
    ]
    assert calls_already_made(killed_mid_turn) == 2, (
        "a resumed turn must start from the calls it already made, not from zero"
    )
    assert calls_already_made(killed_mid_turn) != 3, (
        "the whole thread's count would bound the conversation rather than the turn"
    )
    assert calls_already_made([AIMessage("no human message anywhere")]) == 1
    assert calls_already_made([]) == 0
    assert calls_already_made(None) == 0


def test_the_floor_ties_the_counter_on_an_ordinary_turn_rather_than_trailing_it() -> None:
    """The property that makes reading the thread safe on *every* model call, not just a resume.

    `enforce_loop_cap` takes the `max` of the channel and this floor, so a floor wrong in the high
    direction would cap healthy turns early — the failure a chemist notices and cannot diagnose.

    **The margin this test asserted first does not exist, and that is worth pinning rather than
    quietly correcting.** Both this docstring and two comments in `src/` said the channel "leads
    the floor by exactly one", because the increment writes `calls + 1`. It does — but the
    comparison in `before_model` happens *before* the write, so at the moment the cap is evaluated
    the two are equal. Instrumented over a real default-profile turn at a cap of 4: `0/0`, `1/1`,
    `2/2`, `3/3`, `4/4`. So the `max` is a floor and nothing else, and a floor that ever
    over-counted by one would cap a healthy turn an iteration early with nothing absorbing it.
    """
    thread: list[Any] = [HumanMessage("a question")]
    for call in range(1, 6):
        # The state `before_model` sees for call `call`: the channel holds the calls already
        # authorised (`call - 1`), and the message this call will produce does not exist yet.
        channel_at_comparison = call - 1
        assert calls_already_made(thread) == channel_at_comparison, (
            "the floor must equal the pre-increment channel, not trail it: there is no margin"
        )
        thread.append(AIMessage(f"answer {call}"))
        assert calls_already_made(thread) == call


def test_the_cap_itself_reads_the_floor_and_not_only_the_channel() -> None:
    """Driven through `enforce_loop_cap`, because the helper being right is not the property.

    `calls_already_made` is unit-tested above, and a mutation that deletes the `max` from the cap
    leaves every one of those assertions green — the helper still returns the right number, nothing
    reads it, and a resumed turn gets its fresh budget back. That is the shape this programme has
    already been caught by twice, so the hook is driven here with the two states that differ.

    The decorator wraps the function into a middleware, so the hook is reached as `.before_model`;
    the runtime is unused by it, which is why `None` is enough.
    """
    cap = settings.harness_max_loop_iterations
    resumed = {
        "model_calls": 0,  # the channel, reset by the new run
        "messages": [HumanMessage("this turn"), *[AIMessage(f"call {n}") for n in range(cap)]],
    }
    decision = enforce_loop_cap.before_model(cast(Any, resumed), cast(Any, None))
    assert decision == {"jump_to": "end", "loop_capped": True}, (
        "a resumed turn that already spent the whole budget must stop, not start again at zero"
    )

    fresh = {"model_calls": 0, "messages": [HumanMessage("this turn")]}
    assert enforce_loop_cap.before_model(cast(Any, fresh), cast(Any, None)) == {"model_calls": 1}, (
        "a turn with nothing behind it is unaffected — the floor must not cap a healthy turn"
    )
