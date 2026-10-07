"""The per-turn spend cap meters what a turn bills, and stops it on a compiled graph.

Only a compiled graph shows that the count reaches the state channel (an undeclared channel
drops writes silently) and that the decision reaches the loop. Fakes report `usage_metadata`
the way a provider does; a provider reporting nothing is pinned separately.
"""

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.spend_cap import (
    MeterTurnSpend,
    begin_spend_watch,
    end_spend_watch,
    spend_capped,
    spend_hit_cap,
    turn_billed_tokens,
)
from chemclaw.agent.state import TurnTotal, turn_input
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.tool_registry import registered_tool_names


def _billing(*costs: int) -> list[AIMessage]:
    """One scripted model call per cost, each reporting `usage_metadata` the way a provider does.

    Every call but the last asks for `ls`, so the graph comes back for another call. `total_tokens`
    equals the sum of the parts here, so this fixture cannot tell the two branches of
    `graph_usage_tokens` apart; `tests/test_budget.py` pins that.
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

    The channel exists because `langgraph_agent` passes `create_agent(state_schema=ChemclawState)`;
    `MeterTurnSpend.state_schema` covers graphs compiled without it, which this test cannot reach.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(400))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert result["billed_tokens"] == 400
    assert not spend_capped(result)


def test_a_turn_over_its_budget_is_stopped_and_says_so(
    monkeypatch: pytest.MonkeyPatch, watch: Any
) -> None:
    """Past the budget the graph ends, and the fact is on the state.

    Two calls of 600 against 1,000: the third is refused at 1,200, so the cap bounds spend before
    the next call.
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
    """`model_calls` counts authorisations, not completions.

    `enforce_loop_cap` increments in `before_model`, which no later hook can skip, so a later hook
    ending the run leaves one extra count. That errs early, and is pinned because the field is
    public.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1_000)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    assert result["billed_tokens"] == 1_200, "two calls were billed"
    assert result["model_calls"] == 3, "the third was authorised and never made"


def test_the_partial_answer_still_goes_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A capped turn delivers its partial answer rather than raising it away.

    Unlike upstream's `ModelCallLimitMiddleware`, this cap fabricates no assistant message.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 500)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)
    # The last assistant message is the model's own: the cap fires after a tool result, and nothing
    # may be appended as the assistant, since the CLI, reports and the persisted thread read that
    # slot.
    assistant = [m for m in result["messages"] if isinstance(m, AIMessage)]
    assert assistant[-1].content == "answer 0"


def test_the_cap_binds_with_no_watch_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforcement does not depend on a caller having started a watch.

    The count is a state channel, so the CLI, template steps and tests are capped too.
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
    """A provider that reports no usage meters 0 and the turn runs.

    On such a provider this cap cannot bind; the iteration cap still does.
    """
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 1)
    graph = build_langgraph_agent(
        model=_Model(messages=iter([AIMessage(content="no usage reported")]))
    )

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert not spend_capped(result)
    assert result["messages"][-1].content == "no usage reported"


def test_the_counter_an_operator_reads_is_declared() -> None:
    """The spend-cap counter is declared in the registry, so a deployment can alert on it."""
    assert "chemclaw_turn_spend_caps_total" in METRICS.render()


def test_a_fan_out_shares_one_budget_rather_than_getting_one_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fan-out shares one budget rather than getting one each.

    `task` returns each helper's state as a `Command`, so `billed_tokens` must cross the boundary
    and fold additively. Asserted against the number of calls the fake was asked for.
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
    """A turn landing exactly on its budget is capped: the comparison is `billed >= budget`."""
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

    Derived from the tool registry, so a new module is scanned once its tool is registered.
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
    """No in-tool model call passes its own callbacks.

    A tool body's model call inherits the graph's callbacks, so its usage reaches the stream the cap
    reads; an explicit `config` replaces them and takes the call off the ledger (as
    `off_stream_metering` correctly does for the out-of-graph verifier). Checked per module, since a
    tool may call a model through a helper; a module mixing a tool with a legitimate off-stream call
    should be split.
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
    """The cap reads the turn ledger, not only its own channel.

    `MeterTurnSpend` misses in-tool calls and retried attempts. The ledger is seeded directly,
    since which reading `enforce_spend_cap` trusts is under test.
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
    """With no turn ledger the channel alone still enforces the cap."""
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 500)
    graph = build_langgraph_agent(model=_Model(messages=iter(_billing(600, 600))))

    result = asyncio.run(graph.ainvoke(turn_input("hello")))

    assert spend_capped(result)


# --- the ambient half: what the chemist is told the turn spent -----------------------------------


def _request(billed: int) -> ModelRequest[Any]:
    """A model request whose state carries the `billed_tokens` a branch was handed."""
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        system_prompt=None,
        messages=[],
        tool_choice=None,
        tools=[],
        response_format=None,
        state={"billed_tokens": billed},  # type: ignore[arg-type]
        runtime=None,
    )


def test_a_fan_outs_watch_agrees_with_the_channel_the_cap_reads(watch: Any) -> None:
    """The ambient watch and the state channel fold a fan-out to the same number.

    Branches share a base, so their totals must be folded additively, not by maximum; the watch's
    figure is what the refusal message shows. Driven through the real middleware and channel.
    """
    meter = MeterTurnSpend()
    channel = TurnTotal(int)

    parent = meter.wrap_model_call(_request(0), lambda _r: _billing(1_000)[0])
    channel.update([parent.command.update["billed_tokens"]])
    base = channel.get()
    branches = [
        meter.wrap_model_call(_request(base), lambda _r: _billing(100)[0]),
        meter.wrap_model_call(_request(base), lambda _r: _billing(150)[0]),
    ]
    channel.update([branch.command.update["billed_tokens"] for branch in branches])

    assert channel.get() == 1_250
    assert turn_billed_tokens() == channel.get(), (
        "the chemist is told a different number from the one the cap enforces against"
    )


def test_a_turn_with_no_watch_has_billed_exactly_nothing() -> None:
    """A turn with no watch has billed exactly 0, since the number is shown beside the refusal."""
    assert turn_billed_tokens() == 0


def test_each_cap_marks_its_watch_through_its_own_public_recorder() -> None:
    """`enforce_spend_cap` marks its watch through the public `record_spend_cap`, as the loop cap
    does.
    """
    sources = {
        "spend": (Path("src/chemclaw/agent/spend_cap.py"), "enforce_spend_cap", "record_spend_cap"),
        "loop": (Path("src/chemclaw/agent/loop_cap.py"), "enforce_loop_cap", "record_loop_cap"),
    }
    for cap, (module, enforcer, recorder) in sources.items():
        tree = ast.parse(module.read_text("utf-8"))
        body = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == enforcer
        )
        called = {
            node.func.id
            for node in ast.walk(body)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert recorder in called, (
            f"the {cap} cap's enforcer does not go through {recorder}(), so that recorder has no "
            "producer and the runner's reader is fed by something else"
        )


def test_the_turn_cap_stays_above_what_the_other_two_guards_authorise() -> None:
    """The turn cap stays above what the other two guards authorise.

    A lawful turn is at most `harness_max_loop_iterations` calls of at most
    `agent_context_token_budget` each, and the whole prefix is billed every call. A cap below that
    product silently supersedes the loop cap. This holds the shipped default; a deployment may set a
    smaller cost budget deliberately.
    """
    lawful_ceiling = settings.harness_max_loop_iterations * settings.agent_context_token_budget
    assert settings.agent_max_turn_billed_tokens >= lawful_ceiling, (
        f"the turn cap ({settings.agent_max_turn_billed_tokens:,}) is below the "
        f"{lawful_ceiling:,} tokens that {settings.harness_max_loop_iterations} model calls at "
        f"agent_context_token_budget already authorise, so it refuses lawful turns and makes the "
        "loop cap unreachable"
    )


def test_the_turn_cap_funds_more_calls_than_the_loop_cap_permits() -> None:
    """The turn cap funds more model calls than the loop cap permits, counted in `PREFIX_BOUND`
    calls.
    """
    from tests.test_context_floor import PREFIX_BOUND

    funded = settings.agent_max_turn_billed_tokens // PREFIX_BOUND
    assert funded >= settings.harness_max_loop_iterations, (
        f"the turn cap funds {funded} model calls at PREFIX_BOUND ({PREFIX_BOUND:,} each) while "
        f"the loop cap permits {settings.harness_max_loop_iterations}; the tighter guard is the "
        "one nobody configured"
    )
