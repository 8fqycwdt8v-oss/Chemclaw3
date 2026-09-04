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

import ast
import asyncio
from pathlib import Path
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
from chemclaw.core.tool_registry import registered_tool_names


def _billing(*costs: int) -> list[AIMessage]:
    """One scripted model call per cost, each reporting `usage_metadata` the way a provider does.

    **Every call but the last asks for a tool**, because that is what makes the graph come back for
    another model call — a turn whose first message is prose ends there, and a cap that is only
    ever consulted once cannot be observed to bind. `ls` is the tool because it is registered on
    every agent by `FilesystemMiddleware` and reads the scratchpad, so driving the loop costs the
    test nothing and touches nothing.

    `total_tokens` is stated explicitly rather than left for the reader to derive. It does **not**
    discriminate the two branches of `graph_usage_tokens` — `cost // 2 + (cost - cost // 2)` is
    `cost` identically, so a fixture shaped like this one cannot tell "prefer the provider's total"
    from "sum the parts", and an earlier version of this docstring claimed it could.
    `tests/test_budget.py::test_a_reported_total_is_preferred_and_a_missing_one_is_derived` is
    where that rule is actually pinned.
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

    This is the channel half: the assertion is on the state the run *returns*, because a middleware
    that accumulates perfectly into a channel the graph does not declare is dropped in silence.

    **The declaration that makes it work is `create_agent(state_schema=ChemclawState)` in
    `langgraph_agent`, not `MeterTurnSpend.state_schema`** — and an earlier version of this
    docstring said the opposite. Measured by removing the middleware's own declaration: nothing
    changes, here or in `tests/test_state_channels.py`, because the graph already has the channel.
    The attribute is kept for the case this file cannot reach — a graph compiled around this
    middleware without that argument — but it is not what this test proves, and claiming it was
    made an unfalsifiable statement out of a real lesson.
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


def test_the_call_count_is_what_the_turn_authorised_not_what_it_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`model_calls` counts authorisations, and it says so — because it cannot count completions.

    `enforce_loop_cap` increments in `before_model`, which is the one hook no later middleware can
    skip and therefore the only safe place for the number a cap is compared against
    (`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`). The consequence is
    that a *later* `before_model` hook ending the run — this cap, ordered right after it — leaves
    the increment for a call that never happened: two calls made, three counted.

    Conservative in the direction that matters (a cap can only bind early) and inert for the guard
    itself, which compares the same number it wrote. It is pinned because the field is deliberately
    non-private so a caller may read it off the finished run, and "how many model calls this turn
    made" was what the declaration promised.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    assert result["billed_tokens"] == 1_200, "two calls were billed"
    assert result["model_calls"] == 3, "the third was authorised and never made"


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


def test_the_budget_is_a_ceiling_reached_not_a_ceiling_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that lands exactly on its budget is capped, not waved through.

    `enforce_spend_cap` compares `billed >= budget`, and every other case in this file is strictly
    over — 600 against 500, 1,200 against 1,000 — so `>=` and `>` were indistinguishable and a
    one-character change to the comparison survived the whole suite.

    Exactly-on-budget is the boundary a deployment actually meets, because a budget is usually a
    round number and `agent_max_turn_billed_tokens` is compared against a running total that steps
    through many values. `>=` is the documented intent: the budget is what a turn may spend, so
    having spent it is having reached the ceiling.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    # Two calls of exactly 500 land the total on 1,000 — equal to the budget, never above it.
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(500, 500, 500))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert result["billed_tokens"] == 1_000
    assert spend_capped(result), (
        "a turn that spent exactly its budget was allowed another model call — the comparison is "
        "`>=` deliberately, and `>` would let every round-number budget overrun by one call"
    )


def _modules_that_call_a_model_from_a_tool() -> list[Path]:
    """Every module that both defines a registered tool and builds a model.

    The two halves are what make the pair dangerous: a model call in a module with no tool in it
    runs outside the graph and must meter itself, and a tool in a module that calls no model has
    nothing to take off the stream. Only their intersection is the shape this guard is about.

    Derived from the tool *registry* rather than from a list, so a module added next year is
    scanned the day its tool is registered.
    """
    registered = set(registered_tool_names())
    found: list[Path] = []
    for module in sorted(Path("src/chemclaw").rglob("*.py")):
        tree = ast.parse(module.read_text("utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        }
        builds_model = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_chat_model"
            for node in ast.walk(tree)
        )
        if builds_model and names & registered:
            found.append(module)
    return found


def _model_calls_passing_a_config(module: Path) -> list[int]:
    """The lines in `module` where a model call carries its own `config`, which is the defect."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(module.read_text("utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"ainvoke", "invoke"}
        and any(keyword.arg == "config" for keyword in node.keywords)
    ]


def test_no_in_tool_model_call_passes_its_own_callbacks() -> None:
    """The absence that puts a tool body's model call on the turn's ledger, guarded as an absence.

    **The chain the widened cap depends on is: a tool body calls a model with no `config` → the
    call inherits the graph's callbacks → its usage rides the stream `api/graph_stream` meters →
    `enforce_spend_cap` reads it.** The load-bearing link is the *missing* argument.
    `agent/turn_usage.py` says so in as many words: an explicit `callbacks` config "**replaces**
    the inherited ones rather than joining them", measured there at 55 tokens booked to the ambient
    ledger and 0 seen by the stream. So a well-meaning `config={...}` added to a tool body would
    silently take that call off the ledger and reopen the gap the cap was widened to close, with
    the whole suite green.

    An absence is what `tests/test_upstream_surface.py` asserts for the same reason: nothing else
    can fail when somebody adds the argument back.

    **The module set is derived rather than named**
    (`D-2026-08-29-a-guard-that-names-one-file-guards-one-file`). This scanned
    `agent/condense.py` by name for as long as that was the only tool making a model call, which
    made it a guard over one file rather than over the invariant — a second in-tool model call, of
    exactly the kind an advisor would be, walked past it in silence. And the mistake it would walk
    past is one edit away rather than hypothetical: `agent/verifier.py` passes
    `config=off_stream_metering()` **correctly**, because a judge runs outside the graph where
    nothing else is watching, and `off_stream_metering`'s own docstring says attaching it to an
    in-graph call would take that call off the stream. Copying that line into a tool body is the
    whole defect.

    **What this deliberately does not claim.** The derivation is at *module* granularity, and that
    is deliberate rather than loose: in `agent/condense.py` the `.ainvoke` is in `_read_prose` while
    the registered tool is `condense_protocols`, so a scan of tool bodies would miss the only call
    that exists to be found. The cost is that a module holding both a tool and a legitimately
    off-stream call would read as an offender — none does, and the right answer if one ever should
    is to split the module rather than to loosen this.

    Nor does it prove the inherited callbacks reach the ledger end to end: that is somebody else's
    machinery, exercised by every real turn and pinned by `tests/test_budget.py` on the stream side.
    Writing an end-to-end version was attempted and abandoned, because a scripted fake's streaming
    semantics are not a provider's and the test that resulted would have been evidence about the
    fake. This asserts the one thing in *this* repository that can break the chain.
    """
    scanned = _modules_that_call_a_model_from_a_tool()
    assert scanned, (
        "no module was found that both defines a registered tool and builds a model, so this scan "
        "is asserting nothing — the derivation, not the invariant, is what broke"
    )

    offenders = {
        str(module): lines for module in scanned if (lines := _model_calls_passing_a_config(module))
    }
    assert not offenders, (
        f"{offenders} passes an explicit `config` to a model call "
        "from a module that holds a registered tool. An explicit callbacks config replaces the "
        "inherited ones, taking the call off the turn's stream — so its tokens stop reaching the "
        "ledger `agent/spend_cap.py` enforces against, and that class of spend becomes invisible "
        "to the cap again."
    )


def test_the_cap_reads_the_turn_ledger_not_only_its_own_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enforcement half: spend the cap cannot meter still stops the turn.

    `MeterTurnSpend` books one figure per *model response*, so it is blind to two real classes of
    provider call — a call inside a tool body (proven to reach the ledger by the test above) and
    the discarded first attempt of a call `RepairInvalidToolCalls` retried. Measured before the
    fix: 5,200 tokens spent against a 150-token budget with the cap never firing.

    Seeded directly here, because what this asserts is *which reading `enforce_spend_cap` trusts* —
    the plumbing that fills the ledger is the previous test's subject.
    """
    from chemclaw.agent.turn_usage import TurnUsage, reset_turn_usage, set_turn_usage

    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    usage = TurnUsage()
    token = set_turn_usage(usage)
    try:
        usage.add(TurnUsage(total=5_000))
        graph = build_langgraph_agent(model=_Model(messages=iter(_billing(100, 100))))
        result = asyncio.run(graph.ainvoke(turn_input("hello")))
    finally:
        reset_turn_usage(token)

    # Absent or small: with the ledger already past the budget the cap fires at the first
    # `before_model`, so `MeterTurnSpend` may never run and never write the channel at all. That
    # is the correct outcome — the money was already spent.
    assert result.get("billed_tokens", 0) < 1_000
    assert spend_capped(result), (
        "5,000 tokens were spent where the middleware cannot see them and the cap did not fire"
    )


def test_the_cap_still_binds_with_no_turn_ledger_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off the request path the channel is the only reading, and it still works.

    `metered_turn_tokens()` is 0 for a CLI turn, a template step or a test with no ledger, so `max`
    has to degrade to exactly the previous behaviour rather than to an unbounded turn. This is the
    counter-example that keeps the widening honest.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 500)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
