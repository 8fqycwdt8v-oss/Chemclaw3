"""The iteration cap's durable floor: what a turn has already spent, read off the thread.

The cap channels are `UntrackedValue`, and a resume is a new run of the graph, so without a floor a
turn that dies *n* times would get *n+1* full allowances. `calls_already_made` re-derives the count
from the thread, which survives a pod death, instead of persisting a counter on the hot path; its
consumer is `enforce_loop_cap`.
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
    """A resumed turn does not get a fresh iteration budget.

    Four arms, because each alone would pass for the wrong reason:

    - a resumed turn counts the calls it already made;
    - the count is this turn's, not the conversation's, which would bound a long chat too tightly;
    - a thread with no human message still answers rather than raising;
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
    """On an ordinary turn the floor ties the counter rather than trailing or exceeding it.

    `enforce_loop_cap` takes the `max` of the channel and the floor, and the comparison runs before
    the increment, so the two are equal at evaluation time. A floor that over-counted by one would
    cap a healthy turn an iteration early.
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
    """`enforce_loop_cap` itself reads the floor, not only the channel.

    Unit tests of `calls_already_made` stay green if nothing reads it, so the hook (reached as
    `.before_model`; the runtime is unused, so `None` suffices) is driven with both watch states. A
    live turn always has a watch open, and the watch must join the per-branch floors, not replace
    them, or a turn whose thread holds the whole budget would be allowed one more call. The watch
    starts at 0 so it never lowers a bound the thread established.
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
            # At the cap a graph is owed exactly one tool-less call to write its answer
            # (`loop_cap.AnswerAtTheCap`), so "did not start again from zero" reads as: the one
            # call authorised is that wrap-up, counted past the budget the thread already spent.
            assert decision == {
                "loop_capped": True,
                "loop_wrap_up": True,
                "model_calls": cap + 1,
            }, (
                f"{how}, a resumed turn that already spent the whole budget did not stop: "
                f"{decision}. It started again from the channel's zero"
            )
            ended = enforce_loop_cap.before_model(
                cast(Any, {**resumed, "loop_wrap_up": True}), cast(Any, None)
            )
            assert ended == {"jump_to": "end", "loop_capped": True}, (
                f"{how}, a resumed turn past its wrap-up was allowed another call: {ended}"
            )
        finally:
            if token is not None:
                end_loop_watch(token)
        # The healthy turn is a *different* turn, so it gets its own watch: the wrap-up above is
        # an authorised call and advances the watch past the cap, which is right for every other
        # branch of that turn and would be a cross-turn leak here.
        token = begin_loop_watch() if watching else None
        try:
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
    """A fan-out shares one iteration budget rather than getting one each.

    `SubAgentMiddleware` gives every helper in a batch the same pre-superstep `model_calls`, and
    `TurnTotal` folds branches only afterwards, so without a shared watch each branch spends the
    whole remaining allowance (`1 + W*(cap - 1)` calls). This asserts the number of model calls
    actually made against the cap, and that an ordinary turn still advances once per call.
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
    # One tool-less wrap-up per graph that reaches the cap (`loop_cap.AnswerAtTheCap`) — the
    # supervisor and each helper — is the whole of what the bound pays for an answer. The defect
    # this holds closed is still far outside it: `1 + W*(cap - 1)` is 25 here, against 13.
    graphs = helpers + 1
    assert fanning.calls <= cap + graphs, (
        f"a {helpers}-way fan-out made {fanning.calls} model calls against a cap of {cap} plus "
        f"{graphs} wrap-ups: every branch is spending the whole allowance instead of sharing one"
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
    # The cap's calls, then the one wrap-up the one graph is owed.
    assert plain.calls == cap + 1, (
        f"an ordinary turn made {plain.calls} calls against a cap of {cap} and one wrap-up — the "
        "turn-wide floor is capping a turn that has no siblings"
    )
    assert ordinary.get("model_calls") == cap + 1


def test_every_driver_of_a_turn_binds_a_fan_out_and_not_only_the_front_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every driver of a turn binds a fan-out to one allowance, not only the front door.

    The test above opens the watch itself, so it proves the mechanism, not the wiring. This drives
    `cli.chat.converse`, the real driver, and the control arm (with `turn_caps` neutered) exceeds
    the cap, so the assertion is about wiring.
    """
    from contextlib import nullcontext

    from chemclaw.cli import chat as cli_chat

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
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "task",
                                        "args": {
                                            "description": f"branch {n}",
                                            "subagent_type": "general-purpose",
                                        },
                                        "id": f"t{n}",
                                        "type": "tool_call",
                                    }
                                    for n in range(helpers)
                                ],
                            )
                        )
                    ]
                )
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "write_todos",
                                    "args": {"todos": []},
                                    "id": f"w{self.calls}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

    def _cli_fan_out(model: Any, *, thread: str) -> None:
        graph = build_langgraph_agent(
            model=model, audit_sink=NullAuditSink(), profile=AgentProfile(name="default")
        )
        asyncio.run(cli_chat.converse(graph, "split this several ways", session_id=thread))

    # The shared allowance plus the one tool-less wrap-up each graph that reaches the cap is owed
    # (`loop_cap.AnswerAtTheCap`): the supervisor and every helper.
    bound = cap + helpers + 1
    wired = _FanOut(messages=iter([]))
    _cli_fan_out(wired, thread="cli-fan-out-wired")
    assert wired.calls <= bound, (
        f"a {helpers}-way fan-out through `cli.converse` made {wired.calls} model calls against a "
        f"cap of {cap}: this driver opens no loop watch, so every branch spends the whole allowance"
    )

    unwired = _FanOut(messages=iter([]))
    monkeypatch.setattr(cli_chat, "turn_caps", lambda *a, **k: nullcontext())
    _cli_fan_out(unwired, thread="cli-fan-out-unwired")
    assert unwired.calls > bound, (
        f"with `turn_caps` neutered the same fan-out made {unwired.calls} calls, which is not over "
        f"the bound of {bound} — so this test is not measuring the wiring and would pass with "
        "every driver's watch removed, which is exactly the hole it was written to close"
    )


def test_every_module_that_drives_a_turn_enters_the_shared_cap_manager() -> None:
    """Every module that drives a turn enters `turn_caps`.

    Derived: a module outside `agent/` that calls `build_langgraph_agent` or `build_turn_agent`
    drives a turn and must enter the shared cap manager, so a new driver is covered when written.
    `agent/` is excluded because its builds happen inside an already-stamped turn. The template
    activity has no behavioural cover without a broker, which is why this structural check exists.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    builders = {"build_langgraph_agent", "build_turn_agent"}
    drivers: dict[str, bool] = {}
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).parts[0] == "agent":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        builds = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in builders
            for node in ast.walk(tree)
        )
        if builds:
            drivers[str(path.relative_to(root))] = "turn_caps(" in source

    assert drivers, (
        "no module outside `agent/` calls a graph builder, so this test is asserting nothing — the "
        "builders have been renamed or the drivers have moved"
    )
    missing = sorted(name for name, enters in drivers.items() if not enters)
    assert not missing, (
        f"{missing} build a turn's graph and never enter `agent.turn_ambient.turn_caps`, so there "
        "the loop cap falls back to the per-branch channel snapshot and a `task` fan-out gives "
        "every branch the whole allowance. Two of the three drivers were in this state"
    )
