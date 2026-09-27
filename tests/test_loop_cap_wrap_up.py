"""A turn the loop cap stops still answers, and a helper the cap stops says so.

Driven live against a real model (dl-01, 2026-09-27, both delegation arms): the supervisor sent
three helpers on a six-source sweep, the helpers spent the turn's shared iteration allowance
between them, each handed back its last sentence of narration ("Let me also check…") as its
report, and the supervisor was stopped at its next model call. The chemist got **no answer at
all** — `loop_cap_reached` over an empty turn.

Two halves, asserted on a compiled graph because both live in the wiring:

- **the helper's report says it was cut short** (`agent/tool_result_shape.cut_short_report`, applied
  on the one path both result-rewriting middlewares share), so the caller cannot read narration as
  findings;
- **every graph that reaches the cap gets one tool-less call to write its answer**
  (`loop_cap.enforce_loop_cap` + `loop_cap.AnswerAtTheCap`), and only a second arrival ends it —
  so the supervisor answers the chemist, from what its helpers managed, instead of falling silent.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.loop_cap import (
    WRAP_UP_NOTE,
    begin_loop_watch,
    end_loop_watch,
    enforce_loop_cap,
    loop_capped,
    loop_hit_cap,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.state import turn_input
from chemclaw.core.config import settings

#: What a helper says just before the cap stops it — the dl-01 shape, verbatim.
_NARRATION = "Let me also check one more thing — whether the reaction store has any chlorides."

#: What a graph writes when it is told it is at the cap.
_PARTIAL = "What the sweep supports so far: nothing on chlorides. It was cut short at the limit."


def _loop_call(n: int) -> AIMessage:
    """A reply that narrates and asks for one more tool, which only the cap can stop."""
    return AIMessage(
        content=_NARRATION,
        tool_calls=[
            {"name": "write_todos", "args": {"todos": []}, "id": f"c{n}", "type": "tool_call"}
        ],
    )


class _Sweep(GenericFakeChatModel):
    """Fans out to helpers, loops in every helper, and answers only when told it is at the cap."""

    helpers: int = 3
    calls: int = 0
    wrap_ups: int = 0
    choices: list[Any] = []
    supervisor_saw: list[BaseMessage] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Record the `tool_choice` each binding asks for — the wrap-up must ask for none."""
        self.choices.append(kwargs.get("tool_choice"))
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls += 1
        last = messages[-1]
        if isinstance(last, HumanMessage) and last.content == WRAP_UP_NOTE:
            self.wrap_ups += 1
            if not any(isinstance(m, HumanMessage) and "piece" in str(m.content) for m in messages):
                # The supervisor's thread: keep what it was shown, which is what it answers from.
                self.supervisor_saw[:] = list(messages)
            # A provider that ignored `tool_choice="none"`: text *and* a tool call. The call must
            # be dropped, or `ToolNode` buys a further round of tools past the cap.
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content=_PARTIAL, tool_calls=_loop_call(0).tool_calls)
                    )
                ]
            )
        if self.calls == 1:
            fan = [
                {
                    "name": "task",
                    "args": {"description": f"piece {n}", "subagent_type": "general-purpose"},
                    "id": f"task-{n}",
                    "type": "tool_call",
                }
                for n in range(self.helpers)
            ]
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=fan))]
            )
        return ChatResult(generations=[ChatGeneration(message=_loop_call(self.calls))])


def _run(model: Any) -> tuple[dict[str, Any], bool]:
    """One turn through the real builder, with the turn watch every driver opens."""
    graph = build_langgraph_agent(
        model=model, audit_sink=NullAuditSink(), profile=AgentProfile(name="default")
    )
    token = begin_loop_watch()
    try:
        final = cast(
            dict[str, Any],
            asyncio.run(
                graph.ainvoke(
                    turn_input("sweep everything on aryl chlorides"),
                    {"configurable": {"thread_id": "wrap-up"}},
                )
            ),
        )
        return final, loop_hit_cap()
    finally:
        end_loop_watch(token)


@pytest.fixture
def capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap small enough that three looping helpers spend it, as dl-01's did."""
    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 4)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)


def test_a_turn_whose_helpers_spent_the_cap_still_answers_the_chemist(capped: None) -> None:
    """The dl-01 shape: the supervisor's last word is an answer, not silence.

    Before the fix the supervisor was stopped at its next call and its last `AIMessage` was the
    `task` fan-out, whose text is empty — the turn ended with nothing to say.
    """
    model = _Sweep(messages=iter([]))
    final, capped_turn = _run(model)

    last = final["messages"][-1]
    assert isinstance(last, AIMessage), f"the turn ended on {type(last).__name__}, not an answer"
    assert last.text == _PARTIAL, f"the chemist's answer was {last.text!r}"
    assert not last.tool_calls, "the wrap-up's tool call survived, so a tool could run past the cap"
    assert loop_capped(final) and capped_turn, "the answer is partial and must still be marked so"
    assert "none" in model.choices, "the wrap-up call did not switch tools off"


def test_a_helper_stopped_by_the_cap_reports_that_it_was_cut_short(capped: None) -> None:
    """What the supervisor reads from a capped helper leads with this system's marked statement.

    Before the fix it was the helper's last sentence of narration and nothing else, so the caller
    could not tell "Let me also check…" from a finding.
    """
    model = _Sweep(messages=iter([]))
    _run(model)

    reports = [m for m in model.supervisor_saw if isinstance(m, ToolMessage) and m.name == "task"]
    reports = reports or [m for m in model.supervisor_saw if isinstance(m, ToolMessage)]
    assert len(reports) == model.helpers, f"the supervisor saw {len(reports)} helper reports"
    for report in reports:
        text = str(report.content)
        assert text.startswith("[The helper was stopped by this turn's step limit"), text[:200]
        assert SYSTEM_SPEECH_MARK in text.split("\n\n", 1)[0], "the statement is not marked as ours"
        # The partial findings still reach the caller — the helper's own wrap-up, here.
        assert _PARTIAL in text


def test_the_cap_still_bounds_the_turn_one_wrap_up_per_graph(capped: None) -> None:
    """The bound this costs, stated as a number: the cap, plus one call per graph that reached it.

    A graph whose wrap-up still carried a tool call ends at its next arrival rather than looping,
    which is the second `enforce_loop_cap` branch.
    """
    model = _Sweep(messages=iter([]))
    _run(model)
    graphs = model.helpers + 1
    assert model.wrap_ups == graphs, f"{model.wrap_ups} wrap-ups for {graphs} graphs"
    assert model.calls <= settings.harness_max_loop_iterations + graphs, (
        f"{model.calls} model calls against a cap of {settings.harness_max_loop_iterations} and "
        f"{graphs} graphs — a wrap-up is buying more than one call"
    )


def test_a_second_arrival_at_the_cap_ends_the_graph() -> None:
    """The hook's two branches, driven directly: first arrival authorises, second ends."""
    cap = settings.harness_max_loop_iterations
    spent = {"model_calls": cap, "messages": [HumanMessage("q")]}
    first = enforce_loop_cap.before_model(cast(Any, dict(spent)), cast(Any, None))
    assert first == {"loop_capped": True, "loop_wrap_up": True, "model_calls": cap + 1}
    second = enforce_loop_cap.before_model(
        cast(Any, {**spent, "loop_wrap_up": True}), cast(Any, None)
    )
    assert second == {"jump_to": "end", "loop_capped": True}
