"""The iteration cap's durable floor: what a turn has already spent, read off the thread.

`D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up` measured that the
caps are `UntrackedValue` and a resume is a new run of the graph, so a turn that dies *n* times
gets *n+1* full `harness_max_loop_iterations` allowances. `calls_already_made` is the answer: the
count is re-derived from state that already survives a pod death rather than persisted on the hot
path.

**This file is what is left of the deleted resume test module, and the deletion is the finding.**
The resume module's public surface — `resume_turn`, `resumability`, `Resumability`,
`ResumeOutcome` — had no caller anywhere in `src/`, only its own test file, which is the shape
`CLAUDE.md` names by hand: "a guard with no caller, kept alive by a test that calls it directly, is
the `map_to_hpc_identity` shape — a claim that a control exists". It also shipped three defects a
caller would have hit, recorded in
`D-2026-09-15-a-review-of-the-review-found-the-feature-wrong-from-both-ends`: `resume_turn`
established none of the four per-turn ambients `api/runner.py` sets, and
`plan_gate.enforce_plan_approval` returns
early when `get_current_session_id()` is empty — so every side-effecting tool replayed on a resume
skipped the plan gate; the lease was taken once and never refreshed against a 60 s default, so any
resume longer than a minute reopened the fork the module existed to prevent; and the
`__pregel_tasks` read its own ADR calls a requirement was never written.

`calls_already_made` is kept because it has a real consumer — `enforce_loop_cap` — and closes a
real hole on its own.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.loop_cap import (
    begin_loop_watch,
    calls_already_made,
    end_loop_watch,
    enforce_loop_cap,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import turn_input
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

    **Both watch states, because the one this originally drove is the one production never has.**
    `api/runner.py:1016` calls `begin_loop_watch()` on **every** turn, so a live turn always reaches
    this hook with a watch open — and with no watch open the two per-branch floors are the only
    terms in the comparison, which hides what the third term does to them. Driven: the mutation
    that has the watch *replace* the floors rather than join them (`turn = watch.calls if watch is
    not None else own`) leaves 104 tests green, and at a cap of 25 the no-watch arm still answers
    `{'jump_to': 'end', 'loop_capped': True}` while the same state with a watch answers
    `{'model_calls': 26}` — a turn whose thread already holds the whole budget authorised one more
    call, on every turn a deployment actually serves.

    The watch starts at 0, which is what makes it a *floor* rather than the count: a fresh watch
    must not lower a bound the thread has already established.
    """
    cap = settings.harness_max_loop_iterations
    resumed = {
        "model_calls": 0,  # the channel, reset by the new run
        "messages": [HumanMessage("this turn"), *[AIMessage(f"call {n}") for n in range(cap)]],
    }
    fresh = {"model_calls": 0, "messages": [HumanMessage("this turn")]}

    for watching in (False, True):
        token = begin_loop_watch() if watching else None
        try:
            how = "with a watch open" if watching else "with no watch"
            decision = enforce_loop_cap.before_model(cast(Any, dict(resumed)), cast(Any, None))
            assert decision == {"jump_to": "end", "loop_capped": True}, (
                f"{how}, a resumed turn that already spent the whole budget did not stop: "
                f"{decision}. It started again from the channel's zero"
            )
            allowed = enforce_loop_cap.before_model(cast(Any, dict(fresh)), cast(Any, None))
            assert allowed == {"model_calls": 1}, (
                f"{how}, a turn with nothing behind it was affected ({allowed}) — the floor must "
                "not cap a healthy turn"
            )
        finally:
            if token is not None:
                end_loop_watch(token)


def test_a_fan_out_shares_one_iteration_budget_rather_than_getting_one_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every branch of one turn spends from one allowance — driven as a real `task` fan-out.

    **The defect this holds closed cost 6.25x the cap at the width it was measured on.**
    `SubAgentMiddleware` hands every helper in a batch the *same pre-superstep* `model_calls`, so
    each
    of `W` branches compared the cap against its own private copy of that base and each
    independently
    spent the whole remaining allowance. `TurnTotal` folds the branches additively *afterwards*,
    which
    makes the recorded count right and the bound wrong — the parent only learns the total once every
    branch has finished spending it. Measured at a cap of 4 over 8 helpers: **25** model calls,
    following `1 + W*(cap - 1)`; at the shipped cap of 25 and `agent_max_parallel_tool_calls` of 8,
    193 calls in one turn.

    **Why `tests/test_spend_cap.py::test_a_fan_out_shares_one_budget_rather_than_getting_one_each`
    passed throughout.** It asserts the *fold* is exact — `billed_tokens == calls * per_call` —
    which
    is true of the defect and is what let the claim survive. Nothing compared the turn's real spend
    against the cap under a fan-out, so that is what this asserts: the number of calls the fake was
    actually asked for, against the cap, not the channel against itself.

    The second assertion is the one that stops the fix from being "never let a branch run": an
    ordinary turn with no fan-out is unchanged, and its channel still reports one advance per call
    rather than one per sibling.
    """
    cap = 4
    helpers = 8
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", cap)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    class _FanOut(GenericFakeChatModel):
        calls: int = 0

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            self.calls += 1
            if self.calls == 1:
                fan = [
                    {
                        "name": "task",
                        "args": {"description": f"piece {n}", "subagent_type": "general-purpose"},
                        "id": f"task-{n}",
                        "type": "tool_call",
                    }
                    for n in range(helpers)
                ]
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content="", tool_calls=fan))]
                )
            # Every later call keeps looping, so nothing but the cap can stop the turn.
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_todos",
                                    "args": {"todos": []},
                                    "id": f"c{self.calls}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

    def _drive(model: Any) -> dict[str, Any]:
        graph = build_langgraph_agent(
            model=model, audit_sink=NullAuditSink(), profile=AgentProfile(name="default")
        )
        token = begin_loop_watch()
        try:
            return cast(
                dict[str, Any],
                asyncio.run(
                    graph.ainvoke(
                        turn_input("split this several ways"),
                        {"configurable": {"thread_id": "fan-out-cap"}},
                    )
                ),
            )
        finally:
            end_loop_watch(token)

    fanning = _FanOut(messages=iter([]))
    final = _drive(fanning)
    assert fanning.calls <= cap, (
        f"a {helpers}-way fan-out made {fanning.calls} model calls against a cap of {cap}: every "
        "branch is spending the whole allowance instead of sharing one"
    )
    assert final.get("model_calls") == fanning.calls, (
        f"the channel reports {final.get('model_calls')} for {fanning.calls} real calls — the cap "
        "comparison and the channel advance have been folded into one number"
    )

    class _Plain(_FanOut):
        def _generate(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            self.calls += 1
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_todos",
                                    "args": {"todos": []},
                                    "id": f"p{self.calls}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

    plain = _Plain(messages=iter([]))
    ordinary = _drive(plain)
    assert plain.calls == cap, (
        f"an ordinary turn made {plain.calls} calls against a cap of {cap} — the turn-wide "
        "floor is capping a turn that has no siblings"
    )
    assert ordinary.get("model_calls") == cap
