"""A turn over its billed-token budget still answers: one tool-less call, then the end.

Every case runs on a compiled graph from `graph.ainvoke`, where the three hooks meet in the order
`enforce_loop_cap`, `enforce_spend_cap`, `AnswerAtTheCap`
(`agent/langgraph_agent._harness_middleware`): the order is what decides which cap grants the call,
and only a graph shows it. The hook-level
cases at the end pin the branches a graph cannot reach (a second arrival needs a reply that kept a
tool call, which `AnswerAtTheCap` removes).
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import turn_input
from chemclaw.core.config import settings
from chemclaw.spend.guard import (
    SPEND_WRAP_UP_NOTE,
    WRAP_UP_NOTE,
    begin_spend_watch,
    end_spend_watch,
    enforce_spend_cap,
    loop_capped,
    spend_capped,
    spend_hit_cap,
    turn_billed_tokens,
)

_ANSWER = "What the sweep supports so far: one hit. It was cut short and the rest is unexamined."


class _Turn(GenericFakeChatModel):
    """Loops on `ls` until a wrap-up note arrives, then writes `_ANSWER`.

    Every call reports `per_call` tokens. With `obstinate` the wrap-up reply also keeps a tool call,
    as a provider that ignores `tool_choice="none"` would. `fan_out` makes the first call spawn
    that many helpers.
    """

    per_call: int = 600
    obstinate: bool = False
    fan_out: int = 0
    calls: int = 0
    notes: list[str] = []
    choices: list[Any] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record each binding's `tool_choice`: a wrap-up must ask for none."""
        self.choices.append(kwargs.get("tool_choice"))
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls += 1
        usage = {
            "input_tokens": self.per_call,
            "output_tokens": 0,
            "total_tokens": self.per_call,
        }
        last = messages[-1]
        ls = [{"name": "ls", "args": {"path": "."}, "id": f"c{self.calls}", "type": "tool_call"}]
        if isinstance(last, HumanMessage) and last.content in (WRAP_UP_NOTE, SPEND_WRAP_UP_NOTE):
            self.notes.append(str(last.content))
            message = AIMessage(
                content=_ANSWER, tool_calls=ls if self.obstinate else [], usage_metadata=usage
            )
        elif self.fan_out and self.calls == 1:
            tasks = [
                {
                    "name": "task",
                    "args": {"description": f"piece {n}", "subagent_type": "general-purpose"},
                    "id": f"task-{n}",
                    "type": "tool_call",
                }
                for n in range(self.fan_out)
            ]
            message = AIMessage(content="", tool_calls=tasks, usage_metadata=usage)
        else:
            message = AIMessage(content="looking", tool_calls=ls, usage_metadata=usage)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _run(model: Any) -> tuple[dict[str, Any], int, bool]:
    """One turn through the real builder under the spend watch every driver opens.

    Returns the final state, what the watch says the turn billed, and whether it says capped.
    """
    graph = build_langgraph_agent(
        model=model, audit_sink=NullAuditSink(), profile=AgentProfile(name="default")
    )
    token = begin_spend_watch()
    try:
        final = cast(
            dict[str, Any],
            asyncio.run(
                graph.ainvoke(
                    turn_input("sweep everything on aryl chlorides"),
                    {"configurable": {"thread_id": "spend-wrap-up"}},
                )
            ),
        )
        return final, turn_billed_tokens(), spend_hit_cap()
    finally:
        end_spend_watch(token)


def _last_text(final: dict[str, Any]) -> str:
    last = final["messages"][-1]
    assert isinstance(last, AIMessage), f"the turn ended on {type(last).__name__}, not an answer"
    return str(last.text)


def test_a_turn_over_budget_early_answers_and_bills_the_wrap_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) The spend cap trips first: it grants the one call, and the turn ends after it."""
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    model = _Turn(messages=iter([]), per_call=600)

    final, watched, hit = _run(model)

    assert _last_text(final) == _ANSWER
    assert not cast(AIMessage, final["messages"][-1]).tool_calls
    assert spend_capped(final) and hit
    assert not loop_capped(final), "the step cap did not fire; the turn must not claim it did"
    assert model.calls == 3, "two ordinary calls, then the one wrap-up and nothing after"
    assert final["billed_tokens"] == 3 * 600, "the wrap-up call's bill is in the turn's total"
    assert watched == final["billed_tokens"]
    assert model.notes == [str(SPEND_WRAP_UP_NOTE)], "the call must say the budget, not the steps"
    assert model.choices[-1] == "none"


def test_a_wrap_up_that_keeps_a_tool_call_cannot_buy_another_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A model that ignores `tool_choice="none"` has its calls dropped; the turn still ends."""
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    model = _Turn(messages=iter([]), per_call=600, obstinate=True)

    final, _, _ = _run(model)

    assert model.calls == 3, "a kept tool call bought another round past the budget"
    assert _last_text(final) == _ANSWER
    assert not cast(AIMessage, final["messages"][-1]).tool_calls
    assert spend_capped(final)


def test_a_loop_capped_turn_answers_when_each_call_is_above_the_context_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) Shipped defaults, calls above `agent_context_token_budget`: the loop cap answers.

    The per-call size is the smallest at which the two caps meet in one pass: all but the last
    call allowed are under the spend budget and the last takes the turn to it, so on the arrival
    where the loop cap authorises its wrap-up the spend hook also finds the turn over budget and
    must leave that call alone.
    """
    loop_cap = settings.harness_max_loop_iterations
    budget = settings.agent_max_turn_billed_tokens
    per_call = -(-budget // loop_cap)
    if not (per_call > settings.agent_context_token_budget and (loop_cap - 1) * per_call < budget):
        pytest.skip("the shipped defaults no longer leave a per-call size where both caps meet")
    model = _Turn(messages=iter([]), per_call=per_call)

    final, _, _ = _run(model)

    assert _last_text(final) == _ANSWER, "the loop-capped turn ended with no answer"
    assert loop_capped(final)
    assert model.calls == loop_cap + 1, "the loop cap's own wrap-up is the one extra call"
    assert model.notes == [str(WRAP_UP_NOTE)], "the step cap's wrap-up, not a second one"
    assert not cast(AIMessage, final["messages"][-1]).tool_calls


def test_every_graph_over_budget_gets_one_wrap_up_across_a_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Helpers share `billed_tokens` and keep their own wrap-up: one call per graph, no more.

    Each helper goes over budget on its own thread and answers; the caller comes back over budget,
    with `spend_capped` carried over from its helpers and its own private `loop_wrap_up` unset, and
    still gets its call.
    """
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    helpers = 2
    model = _Turn(messages=iter([]), per_call=400, fan_out=helpers)

    final, _, hit = _run(model)

    assert _last_text(final) == _ANSWER
    assert spend_capped(final) and hit
    assert model.notes == [str(SPEND_WRAP_UP_NOTE)] * (helpers + 1), (
        f"{len(model.notes)} wrap-up calls for {helpers + 1} graphs"
    )


def test_the_first_arrival_grants_and_the_second_ends() -> None:
    """The wrap-up is granted once: the arrival after it is over budget again and ends the turn."""
    over = {"billed_tokens": settings.agent_max_turn_billed_tokens}
    first = enforce_spend_cap.before_model(cast(Any, dict(over)), cast(Any, None))
    assert first == {"spend_capped": True, "loop_wrap_up": True}

    second = enforce_spend_cap.before_model(cast(Any, {**over, **(first or {})}), cast(Any, None))
    assert second == {"jump_to": "end", "spend_capped": True}


def test_the_spend_hook_leaves_the_step_caps_pending_wrap_up_alone() -> None:
    """(a) `loop_wrap_up` without `spend_capped` is the loop cap's call: go on."""
    state = {"billed_tokens": settings.agent_max_turn_billed_tokens, "loop_wrap_up": True}
    assert enforce_spend_cap.before_model(cast(Any, state), cast(Any, None)) is None


def test_a_turn_under_budget_is_never_granted_anything() -> None:
    """Below budget the hook returns nothing, so no flag is set early."""
    state = {"billed_tokens": settings.agent_max_turn_billed_tokens - 1}
    assert enforce_spend_cap.before_model(cast(Any, state), cast(Any, None)) is None
