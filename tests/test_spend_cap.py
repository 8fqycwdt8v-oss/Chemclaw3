"""The per-turn spend cap meters what a turn bills, and stops it on a **compiled graph**.

**Why every assertion here drives a graph rather than a hook.** `agent/loop_cap.py` shipped a cap
whose hook ran, counted correctly, decided correctly, and was wired to nothing — the conditional
edge is built from `can_jump_to`, and its own unit test passed by calling the hook and reading the
returned dict. `tests/test_state_channels.py` exists because the sibling failure is quieter still: a
write to a channel the graph does not declare is dropped in **silence**, so a metering middleware
can accumulate perfectly into nowhere. This module's first probe did exactly that, before it was a
module. So the questions asked here are the two that only a compiled graph can answer: does the
count reach the channel, and does the decision reach the loop.

The model's *reported* usage is what a turn is billed for, so every fake below reports usage the way
a provider does — `usage_metadata` on the message — and the cap is compared against the numbers
those add up to. A fake that reported nothing would make every assertion here vacuous, which is why
`test_a_provider_that_reports_no_usage_cannot_arm_the_cap` pins that case explicitly rather than
leaving it as an accident of the fixtures.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.spend_cap import (
    begin_spend_watch,
    end_spend_watch,
    spend_capped,
    spend_hit_cap,
    turn_billed_tokens,
)
from chemclaw.agent.state import turn_input
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


def _billing(*costs: int) -> list[AIMessage]:
    """One scripted model call per cost, each reporting `usage_metadata` the way a provider does.

    **Every call but the last asks for a tool**, because that is what makes the graph come back for
    another model call — a turn whose first message is prose ends there, and a cap that is only
    ever consulted once cannot be observed to bind. `ls` is the tool because it is registered on
    every agent by `FilesystemMiddleware` and reads the scratchpad, so driving the loop costs the
    test nothing and touches nothing.

    `total_tokens` is set independently of the input/output split rather than derived from it —
    `graph_usage_tokens` prefers the provider's own total, and a fixture that computed it here
    would be asserting this test's arithmetic instead of that rule.
    """
    messages = []
    for index, cost in enumerate(costs):
        usage = {
            "input_tokens": cost // 2,
            "output_tokens": cost - cost // 2,
            "total_tokens": cost,
        }
        if index == len(costs) - 1:
            messages.append(AIMessage(content=f"answer {index}", usage_metadata=usage))
            continue
        messages.append(
            AIMessage(
                content=f"answer {index}",
                tool_calls=[{"name": "ls", "args": {"path": "."}, "id": f"call-{index}"}],
                usage_metadata=usage,
            )
        )
    return messages


class _Model(GenericFakeChatModel):
    """A scripted model that can be bound, because `create_agent` binds tools on every request."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


@pytest.fixture
def watch() -> Any:
    """A turn-scoped spend watch, so the runner-side reader has something to answer from.

    The graph does not need this — the cap is enforced off the state channel — which is the whole
    point of the split, and `test_the_cap_binds_with_no_watch_at_all` proves it by leaving it out.
    """
    token = begin_spend_watch()
    yield
    end_spend_watch(token)


def test_the_meter_reaches_the_channel_and_the_state_carries_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn's bill accumulates across model calls and is readable from what the run returns.

    This is the channel half. Without the `state_schema` declaration on `MeterTurnSpend` the update
    is dropped silently and `billed_tokens` comes back absent — which is what the first attempt at
    this design did, and why the assertion is on the returned state rather than on the middleware.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(400))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert result["billed_tokens"] == 400
    assert not spend_capped(result)


def test_a_turn_over_its_budget_is_stopped_and_says_so(
    monkeypatch: pytest.MonkeyPatch, watch: Any
) -> None:
    """The decision half: past the budget the graph ends, and the fact is on the state.

    Two calls of 600 against a budget of 1,000. The first is made (nothing is booked yet), the
    second is made (600 < 1000), and the *third* is refused at 1,200 — so the cap is a ceiling on
    what a turn may spend before its next call, which is what `enforce_spend_cap` documents and the
    only placement that bounds anything.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    assert result["billed_tokens"] == 1_200
    # The runner's reader agrees with the state's, which is what lets a streaming driver — which
    # never gets the final state back — report the same fact.
    assert spend_hit_cap()
    assert turn_billed_tokens() == 1_200


def test_the_partial_answer_still_goes_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A capped turn delivers what it managed, rather than raising it away.

    The position `agent/loop_cap.py` argues and this module inherits: a chemist is entitled to see
    the work the last iteration managed. Upstream's `ModelCallLimitMiddleware` fabricates an
    assistant message in this slot, which is one of the four regressions that got it reverted; this
    cap emits none, so the last real answer is still the last message.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 500)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    # The model's own last words survive. The assertion is on the last *assistant* message rather
    # than the last message, because the cap fires in `before_model` after the tool result that
    # provoked the next call — so the tail of the thread is that result, and what matters is that
    # nothing was appended *as the assistant*. Upstream's `ModelCallLimitMiddleware` fabricates an
    # `AIMessage` carrying its limit string in exactly this slot, and `cli/chat.py`, the helper
    # report and the persisted thread all read that position.
    assistant = [m for m in result["messages"] if isinstance(m, AIMessage)]
    assert assistant[-1].content == "answer 0"


def test_the_cap_binds_with_no_watch_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforcement does not depend on a caller having started a watch.

    The reason the count is a state channel rather than a contextvar. An ambient ledger would make
    the cap inert everywhere nobody remembered to start one — the CLI, a template step, a test —
    which is the "per-turn is a property of every call site" mistake `agent/state.py` records.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 500)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    # No watch was started, so the runner-side reader is simply False rather than wrong.
    assert not spend_hit_cap()


def test_an_unset_budget_never_stops_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 means no cap — the shipped default, and the convention `budget.py::_over` already uses."""
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(10**6, 10**6))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert not spend_capped(result)
    # Both calls were made and both were booked: the meter keeps counting with the cap switched
    # off, which is what leaves `turn_costs` and the counters intact for a deployment that has not
    # sized a budget yet.
    assert result["billed_tokens"] == 2 * 10**6


def test_a_provider_that_reports_no_usage_cannot_arm_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model reporting nothing meters 0 and the turn runs — it must not fail instead.

    `turn_usage.graph_usage_tokens` duck-types on the provider's keys precisely so that a provider
    or version reporting no usage meters zero rather than failing a turn. The honest cost is
    recorded here rather than left to be discovered: on such a provider this cap cannot bind at
    all, and the guard that still does is the iteration cap.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1)
    graph = build_langgraph_agent(
        model=_Model(messages=iter([AIMessage(content="no usage reported")]))
    )

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert not spend_capped(result)
    assert result["messages"][-1].content == "no usage reported"


def test_the_counter_an_operator_reads_is_declared() -> None:
    """The metric exists in the registry, so a deployment can alert on the guard firing.

    Declared-ness is the assertion, not a count: `core/metrics.py` refuses an undeclared series, so
    a counter the runner increments and nobody declared would fail at the increment rather than
    here — and a rename would pass a test that only asserted the increment.
    """
    assert "chemclaw_turn_spend_caps_total" in METRICS.render()


def test_a_fan_out_shares_one_budget_rather_than_getting_one_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper's spend lands on the *turn's* total, which is why the count is a channel.

    **This is the claim that justifies the whole design over an ambient ledger**, and it is the one
    a unit test cannot make: `task` returns each helper's final state as a `Command` update, so the
    budget spans the team only if `billed_tokens` crosses the subagent boundary and its channel
    folds concurrent writes additively. Regression 3 in `agent/loop_cap.py`'s list is what happens
    when it does not — every branch starts at zero and an N-way fan-out gets N times the budget it
    was given.

    Modelled on `tests/test_subagents.py`'s fan-out case, and asserted the same way: against the
    number of calls the fake was actually asked for, so an under-count is a failure rather than a
    smaller number nobody checks. One parent call to fan out, one per helper, one to answer.
    """
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.profiles import AgentProfile

    monkeypatch.setattr(settings, "harness_enabled", True)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)
    per_call = 300
    helpers = 2

    class _FanOut(GenericFakeChatModel):
        """Spawns two helpers at once, then answers — every call reporting the same usage."""

        calls: int = 0

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            """Accept the binding; the script does not reason about tools."""
            return self

        def _generate(
            self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> Any:
            """One fan-out message, then prose — each carrying `per_call` billed tokens."""
            from langchain_core.outputs import ChatGeneration, ChatResult

            self.calls += 1
            usage = {"input_tokens": per_call, "output_tokens": 0, "total_tokens": per_call}
            if self.calls == 1:
                message = AIMessage(
                    content="",
                    usage_metadata=usage,
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": f"piece {i}",
                                "subagent_type": "general-purpose",
                            },
                            "id": f"task-{i}",
                            "type": "tool_call",
                        }
                        for i in range(helpers)
                    ],
                )
            else:
                message = AIMessage(content=f"answer {self.calls}", usage_metadata=usage)
            return ChatResult(generations=[ChatGeneration(message=message)])

    model = _FanOut(messages=iter([]))
    graph = build_langgraph_agent(
        model=model, audit_sink=NullAuditSink(), profile=AgentProfile(name="default")
    )

    final = asyncio.run(graph.ainvoke(turn_input("split this in two")))

    assert model.calls == helpers + 2, "the fake was not driven the way this test assumes"
    assert final["billed_tokens"] == model.calls * per_call, (
        f"{model.calls} calls billed {model.calls * per_call} and "
        f"{final['billed_tokens']} were counted — a fan-out that under-counts gives every helper "
        "its own share of one budget"
    )
