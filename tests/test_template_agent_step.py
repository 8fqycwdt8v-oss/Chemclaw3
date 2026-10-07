"""The `agent` step's surface: ungated by the plan gate, and read-only unless the file says so.

A template is itself the reviewed, pre-approved plan, so an `agent` step is not plan-gated;
instead every side-effecting tool the step did not declare is removed before the agent is built.
The narrowing must reach the connector specs as well as the builder's profile, so the headline
test drives the real activity, graph and `connector_specs`. A second group checks an `agent` step
is instrumented like a chat turn: ambient session, token counters and the `turn_costs` ledger.
"""

import asyncio
import contextlib
import os
import subprocess
import sys
import typing
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool as tool_decorator
from temporalio.testing import ActivityEnvironment

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import advertised_tool_names
from chemclaw.agent.framing import SYSTEM_SPEECH_MARK
from chemclaw.agent.state import answer_text
from chemclaw.agent.tool_result_size import STEP_REMEDY, TOOL_REMEDY
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.durable.template_activities import (
    AgentStepInput,
    AgentStepResult,
    StepIdentity,
    ToolStepInput,
    step_profile,
)
from chemclaw.durable.template_job import run_summary, template_job_record
from chemclaw.templates.manifest import AgentStep
from tests.fakes_langgraph import ScriptedChatModel

# A `calc` endpoint tool the manifest classifies `state_changing`. The whole point of testing with
# this one rather than an in-process write: it lives on the *other* side of the surface, which is
# the half a narrowing applied to only the builder's profile leaves wide open.
_CONNECTOR_WRITE = "compute_xtb_energy"

# What one scripted model call reports. Split rather than a bare total because the counters are
# split, and a test that only checked the total would pass with the four dimensions all publishing
# the same number.
_USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "input_token_details": {},
}

_SPEND_COUNTERS = (
    "chemclaw_tokens_total",
    "chemclaw_input_tokens_total",
    "chemclaw_output_tokens_total",
)


class _Recorder:
    """An audit sink that keeps what it is handed."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        """Keep one event."""
        self.events.append(event)


def _stand_in(name: str, calls: list[str]) -> Any:
    """One connector tool as `open_connector_specs` produces them: an ordinary LangChain tool.

    It records its name when its body runs, since the claim is that the write did not happen.
    """

    @tool_decorator(name_or_callable=name, description=f"stand-in for {name}")
    async def _fake(smiles: str) -> str:
        calls.append(name)
        return f"{name} ran"

    return _fake


def _scripted(script: list[Any] | ScriptedChatModel) -> ScriptedChatModel:
    """The step's model: the shared fake's script shorthand, or ready-made messages.

    The shorthand cannot carry `usage_metadata`, which the metering tests need, so `AIMessage`
    scripts go to the fake's `messages` iterator instead.
    """
    if isinstance(script, ScriptedChatModel):
        # Already a model: a test that needs the provider itself to misbehave (a mid-turn outage)
        # cannot express that as a script, because the behaviour under test is the *absence* of a
        # further message rather than its content.
        return script
    if script and isinstance(script[0], AIMessage):
        return ScriptedChatModel(messages=iter(script))
    return ScriptedChatModel(script)


class _Step(NamedTuple):
    """Everything one driven step is observable by — see `_drive`."""

    answer: str
    calls: list[str]
    events: list[Any]
    offered: list[str]
    costs: list[TurnCost]
    # The whole `AgentStepResult`, because the answer text is now only *part* of what the step
    # returns and the rest of it — what was unreachable, how the turn ended — is the thing the
    # degradation group below is about.
    result: AgentStepResult


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    step: AgentStepInput,
    script: list[Any] | ScriptedChatModel,
    unreachable: list[str] | None = None,
) -> _Step:
    """Run the real `run_agent_step` against a scripted model, and report what happened.

    Three substitutions, none on the path under test:

    - `llm_provider.build_chat_model`, so the production wiring runs;
    - `open_connector_specs`, since no MCP server runs here; the stand-in builds one tool per name
      each spec's allow-list actually carries, which makes this a test of the narrowing;
    - the turn-cost sink, so the ledger row is observable; `record_turn_cost` writes from an
      unawaited task, so the run yields once afterwards.

    Returns the step's answer, the tool bodies that ran, the audit events, every tool name the specs
    advertised, and the cost rows booked.
    """
    from chemclaw.durable import template_activities

    calls: list[str] = []
    offered: list[str] = []
    costs: list[TurnCost] = []
    sink = _Recorder()
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: sink)
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_a, **_k: _scripted(script),
    )
    monkeypatch.setattr(
        "chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: _CostRecorder(costs)
    )

    async def fake_open(_stack: AsyncExitStack, specs: Any) -> tuple[list[Any], list[str]]:
        names = [name for spec in specs for name in (spec.allowed_tools or [])]
        offered.extend(names)
        # The second element is what a real `open_connector_specs` reports as *not* opened, and
        # `unreachable` is how a test asks for that half — a dark bundle contributes no tools, so
        # there is nothing else about it a caller could observe.
        return [_stand_in(name, calls) for name in names], list(unreachable or [])

    monkeypatch.setattr(template_activities, "open_connector_specs", fake_open)

    async def _run() -> AgentStepResult:
        result = await template_activities.run_agent_step(step)
        # One scheduling round is enough for a recorder that never awaits anything real; the point
        # is only that the cost task gets to run before the loop `asyncio.run` closes it.
        await asyncio.sleep(0)
        # The activity is annotated `AgentStepResult | str` for the rollout window it documents;
        # *this* code path is always the current one, so narrowing here is an assertion rather
        # than a cast.
        assert isinstance(result, AgentStepResult), result
        return result

    outcome = asyncio.run(_run())
    return _Step(outcome.answer, calls, sink.events, offered, costs, outcome)


class _CostRecorder:
    """A turn-cost sink that keeps the rows instead of writing them.

    Not `NullTurnCostSink`, deliberately: `record_turn_cost` short-circuits on that type, so a test
    using it would observe nothing and would also skip the scheduling this asserts happens at all.
    """

    def __init__(self, rows: list[TurnCost]) -> None:
        self.rows = rows

    async def record(self, cost: TurnCost) -> None:
        """Keep one cost row."""
        self.rows.append(cost)


def _step(**overrides: Any) -> AgentStepInput:
    """One `agent` step input, defaulting to the read-only shape a template gets for free."""
    payload: dict[str, Any] = {
        "prompt": "brief me on CCO",
        "step_id": "brief",
        "identity": StepIdentity(
            actor="chemist-1",
            roles=[],
            correlation_id="template-run-1",
            # The launching chat. Carried by every real service-path run (`TemplateRunInput`), and
            # the whole point of the metering group below is that it reaches the audit trail.
            session_id="s-tmpl",
        ),
    }
    payload.update(overrides)
    return AgentStepInput(**payload)


# --- the headline: an undeclared write does not happen -------------------------------------------


def test_an_undeclared_write_never_runs_and_the_step_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undeclared write never runs, and the step still answers.

    1. the write's body never ran;
    2. the attempt is an audit row with `outcome="refused"` and a `detail` saying why, since
       `ToolNode`'s invalid-name message would also book a failure row, listing the agent's
       inventory;
    3. the connector specs the step opened never advertised it;
    4. the turn still answers, with the refusal as the call's result rather than a retryable error.
    """
    run = _drive(
        monkeypatch,
        _step(),
        [{"name": _CONNECTOR_WRITE, "args": {"smiles": "CCO"}}, "no flags matched"],
    )

    assert run.calls == [], f"an undeclared write executed: {run.calls}"
    assert _CONNECTOR_WRITE not in run.offered, (
        "the step opened connectors still advertising the write — the profile is being resolved "
        f"twice, and only the builder's copy is narrowed; offered: {sorted(set(run.offered))}"
    )
    (refused,) = [e for e in run.events if e.tool == _CONNECTOR_WRITE]
    assert (refused.outcome, refused.actor) == ("refused", "chemist-1"), refused
    assert "UndeclaredWriteRefusal" in refused.detail, refused.detail
    assert "not a valid tool" not in refused.detail, refused.detail
    assert run.answer == "no flags matched"


def test_the_model_reads_a_refusal_rather_than_a_retryable_error() -> None:
    """The model reads a refusal rather than a retryable error.

    LangGraph's answer to an unbound name is `ToolMessage(status="error")` inviting a retry and
    listing the inventory; a deliberately withheld tool gets `_refusal_message`'s refusal instead.
    """
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.state import turn_config, turn_input

    graph = build_langgraph_agent(
        ScriptedChatModel(
            [{"name": "record_knowledge_note", "args": {"title": "x"}}, "could not do that"]
        ),
        profile=step_profile(None, []),
        audit_sink=NullAuditSink(),
    )
    result = asyncio.run(graph.ainvoke(turn_input("write it up"), turn_config()))

    (message,) = [m for m in result["messages"] if getattr(m, "type", "") == "tool"]
    assert message.status != "error", "a deliberate refusal must not reach the model as is_error"
    assert message.text.startswith("Refused: record_knowledge_note changes stored data"), (
        message.text
    )
    assert "try one of" not in message.text


def test_a_declared_write_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared write is reachable, so the narrowing is a declaration rather than a ban.

    Only one template line differs from the test above, which pins that the connector spec carries
    the difference.
    """
    run = _drive(
        monkeypatch,
        _step(write_tools=[_CONNECTOR_WRITE]),
        [{"name": _CONNECTOR_WRITE, "args": {"smiles": "CCO"}}, "done"],
    )

    assert run.calls == [_CONNECTOR_WRITE]
    assert _CONNECTOR_WRITE in run.offered
    assert [e.outcome for e in run.events if e.tool == _CONNECTOR_WRITE] == ["ok"]
    assert run.answer == "done"


def test_a_read_tool_stays_reachable_without_any_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is read-only, not tool-less — a step that cannot look anything up is useless.

    `screen_hazards` is a `safety` endpoint tool the manifest classifies `read_only`, so it survives
    the subtraction with nothing declared.
    """
    run = _drive(
        monkeypatch,
        _step(),
        [{"name": "screen_hazards", "args": {"smiles": "CCO"}}, "no flags"],
    )

    assert "screen_hazards" in run.offered
    assert run.calls == ["screen_hazards"]
    assert run.answer == "no flags"


# --- the step is a model turn, so it is metered like one ------------------------------------------


def _metered_script() -> list[AIMessage]:
    """A two-call turn, a tool call then an answer, each reporting provider usage.

    Two calls, because metering only the final message would make tool calls free.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": "screen_hazards", "args": {"smiles": "CCO"}, "id": "call-1"}],
            usage_metadata=_USAGE,
        ),
        AIMessage(content="no flags", usage_metadata=_USAGE),
    ]


def test_the_audit_row_names_the_session_the_run_was_launched_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit row names the session the run was launched from.

    `agent/audit.py` reads `get_current_session_id()`, so the step must stamp it. Asserted on a tool
    row written from inside the graph, so the stamp is shown to survive the whole call depth.
    """
    run = _drive(monkeypatch, _step(), _metered_script())

    (event,) = [e for e in run.events if e.tool == "screen_hazards"]
    assert (event.actor, event.session_id) == ("chemist-1", "s-tmpl"), event


def test_the_steps_tokens_reach_the_counters_the_deployment_bills_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step's tokens reach the counters the deployment bills from.

    Asserted as a delta, since `METRICS` is process-wide. The split counters are checked beside the
    total because they are priced separately.
    """
    before = {name: METRICS.value(name) for name in _SPEND_COUNTERS}
    _drive(monkeypatch, _step(), _metered_script())
    after = {name: METRICS.value(name) for name in _SPEND_COUNTERS}

    moved = {name: after[name] - before[name] for name in _SPEND_COUNTERS}
    assert moved == {
        # Two model calls at 120 total / 100 input / 20 output each.
        "chemclaw_tokens_total": 240.0,
        "chemclaw_input_tokens_total": 200.0,
        "chemclaw_output_tokens_total": 40.0,
    }, moved


class _ProviderOutage(ScriptedChatModel):
    """A model that serves `paid` calls and then fails, counting what the provider billed.

    Counted provider-side because a failing turn has no `result["messages"]` to read.
    """

    served: int = 0

    # `*args, **kwargs` on both, deliberately: upstream calls `_generate` with a run manager and
    # `_stream` with a different arity, and pinning either signature here would make this fake fail
    # on a LangChain bump for a reason that has nothing to do with what it tests.
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        self._bill_or_fail()
        return super()._generate(*args, **kwargs)

    def _stream(self, *args: Any, **kwargs: Any) -> Any:
        self._bill_or_fail()
        yield from super()._stream(*args, **kwargs)

    def _bill_or_fail(self) -> None:
        """Serve two calls the provider would have charged for, then fail the turn."""
        if self.served >= 2:
            raise RuntimeError("provider 529 / worker evicted mid-turn")
        self.served += 1


def test_a_step_whose_provider_fails_still_books_the_calls_it_already_paid_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step whose provider fails still books the calls it already paid for.

    Spend accumulates in a callback as each call ends, so an exception after two paid calls does not
    book an all-zero row.
    """
    # Two *tool-call* turns, so the graph still wants a third model call when the provider dies:
    # a script ending in an answer would finish the turn and never reach the outage.
    paid = [
        AIMessage(
            content="",
            tool_calls=[{"name": "screen_hazards", "args": {"smiles": "CCO"}, "id": f"call-{n}"}],
            usage_metadata=_USAGE,
        )
        for n in (1, 2)
    ]
    model = _ProviderOutage(messages=iter(paid))
    before = METRICS.value("chemclaw_tokens_total")
    with pytest.raises(RuntimeError):
        _drive(monkeypatch, _step(), model)
    moved = METRICS.value("chemclaw_tokens_total") - before

    assert model.served == 2, "the provider billed exactly the calls this asserts"
    assert moved == 240.0, f"two paid calls booked as {moved}"


def test_a_successful_step_books_each_model_call_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful step books each model call exactly once.

    The callback replaces the post-hoc sum rather than joining it; asserted as `calls x per-call
    usage`, so neither doubling nor dropping passes.
    """
    before = METRICS.value("chemclaw_tokens_total")
    run = _drive(monkeypatch, _step(), _metered_script())
    moved = METRICS.value("chemclaw_tokens_total") - before

    assert run.answer == "no flags"
    # Two calls at 120 total each, spelled out the way the sibling counter test does.
    assert moved == 240.0, f"two calls at 120 booked as {moved}"


def test_the_step_writes_a_cost_row_attributed_to_the_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step writes a cost row attributed to the requester.

    `chemclaw_tokens_total` is labelled by profile only, so per-person spend lives in `turn_costs`.
    The row key is the run's correlation id plus the step id, since `turn_costs` upserts on it and
    every step of a run shares the run's id.
    """
    run = _drive(monkeypatch, _step(), _metered_script())

    (cost,) = run.costs
    assert (cost.actor, cost.session_id) == ("chemist-1", "s-tmpl")
    assert cost.correlation_id == "template-run-1:brief", (
        "the cost row is keyed on the run alone, so a second agent step would upsert over this one"
    )
    assert (cost.input_tokens, cost.output_tokens) == (200, 40)
    assert cost.completed is True
    assert cost.duration_seconds > 0


# --- the resolved profile, and what ships ---------------------------------------------------------


def test_the_resolved_step_profile_holds_no_write_it_was_not_given() -> None:
    """The resolved step profile holds no write it was not given, over the live registry.

    Asserted against `side_effecting_tools()`, the set the dry-run guard and plan gate use, since
    only a bundle's manifest knows which of its tools spend compute.
    """
    profile = step_profile(None, [])

    assert profile.harness_enabled is False, "an agent step must not run the plan/execute harness"
    assert profile.tool_names is not None
    assert not (profile.tool_names & side_effecting_tools()), sorted(
        profile.tool_names & side_effecting_tools()
    )
    # Attenuation only: a step can never hold something its profile did not already advertise.
    assert profile.tool_names <= advertised_tool_names(None)
    # And it is not empty, which is the way a "narrowing" passes every test while breaking the step.
    assert len(profile.tool_names) > 10, sorted(profile.tool_names)


def test_a_declaration_cannot_widen_past_the_profile() -> None:
    """Naming a tool the profile never advertised grants nothing (`make template-validate` says so).

    Pinned here as well as in the validator because the validator is a gate a person can be told to
    ignore, and this is the behaviour it describes.
    """
    profile = step_profile(None, ["not_a_tool_anything_provides"])

    assert profile.tool_names is not None, "a step with no narrowing has nothing to widen past"
    assert "not_a_tool_anything_provides" not in profile.tool_names


def test_the_shipped_hazard_briefing_step_declares_no_writes() -> None:
    """The one template that ships must not have been broadened to make the change pass.

    Its `brief` step calls nothing — it turns two earlier steps' results into prose — so its
    surface is the read-only default, and the resolved profile is disjoint from every write.
    """
    from chemclaw.templates.registry import discovered

    template = discovered()["hazard-briefing"]
    (agent_step,) = [step for step in template.steps if isinstance(step, AgentStep)]

    assert agent_step.write_tools == []
    resolved = step_profile(agent_step.profile, agent_step.write_tools)
    assert resolved.tool_names is not None
    assert not (resolved.tool_names & side_effecting_tools())


def test_the_sequencer_hands_the_step_its_declared_writes() -> None:
    """The sequencer hands the step its declared writes.

    The tests above build `AgentStepInput` by hand, so only this shows `_run_step` passes
    `write_tools`. The module's `workflow` handle is substituted, since the real API refuses to run
    outside a workflow event loop.
    """
    import types
    from datetime import timedelta

    from chemclaw.durable import template_job

    sent: list[Any] = []

    async def execute_activity(_activity: Any, payload: Any, **_kwargs: Any) -> str:
        sent.append(payload)
        return "ok"

    step = AgentStep(id="brief", prompt="write it up", write_tools=["record_knowledge_note"])
    identity = StepIdentity(actor="chemist-1", roles=[], correlation_id="run-1")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            template_job, "workflow", types.SimpleNamespace(execute_activity=execute_activity)
        )
        asyncio.run(
            template_job.TemplateWorkflow()._run_step(
                step, {}, identity, timedelta(seconds=60), "probe"
            )
        )

    (payload,) = sent
    assert payload.write_tools == ["record_knowledge_note"]


def test_every_dispatched_step_carries_a_heartbeat_timeout() -> None:
    """Every dispatched step carries a heartbeat timeout.

    Temporal reacts to heartbeats only with a `heartbeat_timeout`; without one a dead worker is
    noticed only when `start_to_close` lapses. Both step kinds in one test, since the failure mode
    is a kind arriving without it. The `workflow` handle is substituted as in the test above.
    """
    import types
    from datetime import timedelta

    from chemclaw.core.config import settings
    from chemclaw.durable import template_job
    from chemclaw.templates.manifest import ToolStep

    options: list[dict[str, Any]] = []

    async def execute_activity(_activity: Any, _payload: Any, **kwargs: Any) -> str:
        options.append(kwargs)
        return "ok"

    identity = StepIdentity(actor="chemist-1", roles=[], correlation_id="run-1")
    steps = [
        ToolStep(id="screen", tool="screen_hazards", arguments={}),
        AgentStep(id="brief", prompt="write it up"),
    ]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            template_job, "workflow", types.SimpleNamespace(execute_activity=execute_activity)
        )
        for step in steps:
            asyncio.run(
                template_job.TemplateWorkflow()._run_step(
                    step, {}, identity, timedelta(seconds=60), "probe"
                )
            )

    expected = timedelta(seconds=settings.template_step_heartbeat_timeout_seconds)
    assert [o.get("heartbeat_timeout") for o in options] == [expected, expected], options
    # The per-attempt budget stays beside it: a heartbeat bounds silence, not the work.
    assert [o.get("start_to_close_timeout") for o in options] == [
        timedelta(seconds=60),
        timedelta(seconds=60),
    ]


# --- and the beat the option is listening for ----------------------------------------------------


# A `safety` endpoint tool classified `read_only`, so it survives an `agent` step's narrowing and
# passes a `tool` step's authorization. The stand-in borrows the name; the subject is the wrapper
# around the wait.
_SLOW_TOOL = "screen_hazards"
# The heartbeat timeout the two activities get here. `beating` beats at a quarter of it, floored at
# one second, so four is the smallest value exercising the shipped arithmetic.
_TEST_HEARTBEAT_TIMEOUT_SECONDS = 4
# How long the driven work waits to be heartbeat for before giving up and answering anyway. It
# bounds only the *failing* run: a healthy step is released by the beat itself, so a pass costs one
# beat interval and no more.
_BEAT_DEADLINE_SECONDS = 10.0


class _Beats:
    """Every heartbeat one driven activity emitted, and the release the driven work waits on."""

    def __init__(self) -> None:
        self.seen: list[Any] = []
        self._first = asyncio.Event()

    def record(self, *details: Any) -> None:
        """`ActivityEnvironment.on_heartbeat`: keep the beat, and let the waiting work finish."""
        self.seen.append(details)
        self._first.set()

    async def wait(self) -> None:
        """Block until this activity has heartbeat, or until the deadline lapses.

        Waiting for the beat rather than sleeping is quick and race-free; the deadline is suppressed
        so the failure is the assertion below, not a `TimeoutError` inside the activity.
        """
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._first.wait(), _BEAT_DEADLINE_SECONDS)


def _beats_of(monkeypatch: pytest.MonkeyPatch, activity: Any, payload: Any) -> _Beats:
    """Run one real template step activity, in an activity context, over one slow tool.

    The tool does not return until a heartbeat arrives, so the timer is observable from outside;
    `ActivityEnvironment` makes `activity.heartbeat` legal. Substitutions are `_drive`'s plus the
    heartbeat timeout.
    """
    from chemclaw.core.config import settings
    from chemclaw.durable import template_activities

    beats = _Beats()
    monkeypatch.setattr(
        settings, "template_step_heartbeat_timeout_seconds", _TEST_HEARTBEAT_TIMEOUT_SECONDS
    )
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: _Recorder())
    monkeypatch.setattr(
        "chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: _CostRecorder([])
    )
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_a, **_k: _scripted([{"name": _SLOW_TOOL, "args": {"smiles": "CCO"}}, "done"]),
    )

    @tool_decorator(name_or_callable=_SLOW_TOOL, description=f"slow stand-in for {_SLOW_TOOL}")
    async def _slow(smiles: str) -> str:
        await beats.wait()
        return "screened"

    async def fake_open(_stack: AsyncExitStack, _specs: Any) -> tuple[list[Any], list[str]]:
        return [_slow], []

    monkeypatch.setattr(template_activities, "open_connector_specs", fake_open)

    env = ActivityEnvironment()
    env.on_heartbeat = beats.record

    async def _driven() -> None:
        await env.run(activity, payload)
        # The cost task `run_agent_step` deliberately does not await, given one scheduling round
        # before the loop closes under it — same reason as `_drive`.
        await asyncio.sleep(0)

    asyncio.run(_driven())
    return beats


@pytest.mark.parametrize(
    ("activity_name", "payload"),
    [
        pytest.param(
            "run_tool_step",
            lambda: ToolStepInput(
                tool=_SLOW_TOOL,
                arguments={"smiles": "CCO"},
                identity=StepIdentity(actor="chemist-1", roles=[], correlation_id="run-1"),
            ),
            id="tool",
        ),
        pytest.param("run_agent_step", lambda: _step(prompt="screen CCO"), id="agent"),
    ],
)
def test_every_dispatched_step_actually_heartbeats(
    monkeypatch: pytest.MonkeyPatch, activity_name: str, payload: Any
) -> None:
    """Every dispatched step actually heartbeats.

    A `heartbeat_timeout` is a deadline, so a step that never beats would be killed after it.
    Observed through `ActivityEnvironment.on_heartbeat`, the environment's own record, for both step
    kinds.
    """
    from chemclaw.durable import template_activities

    beats = _beats_of(monkeypatch, getattr(template_activities, activity_name), payload())

    assert beats.seen, (
        f"{activity_name} ran for {_BEAT_DEADLINE_SECONDS:.0f}s without one heartbeat. Temporal "
        "was told to expect one within `template_step_heartbeat_timeout_seconds`, so this step is "
        "killed as a dead worker the moment it outlives that — restore `beating(...)` around the "
        "wait."
    )


# --- the validator ------------------------------------------------------------------------------


def _problems(write_tools: list[str], profile: str | None = None) -> list[str]:
    """Every problem the validator reports for one agent step declaring `write_tools`."""
    from chemclaw.agent.template_surface import available_tools, step_problems
    from chemclaw.templates.manifest import Template

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Write something up.",
            "steps": [
                {
                    "id": "brief",
                    "kind": "agent",
                    "prompt": "write it up",
                    "profile": profile,
                    "write_tools": write_tools,
                }
            ],
        }
    )
    available_tools()  # the in-process registry is an import side effect; see the validator
    return step_problems(template)


def test_declaring_a_read_tool_as_a_write_is_a_problem() -> None:
    """Declaring a read tool as a write is a problem.

    A read tool needs no declaration, and accepting it would let the write list grow into a general
    allow-list.
    """
    (problem,) = _problems(["screen_hazards"])

    assert "changes nothing" in problem
    assert "screen_hazards" in problem


def test_declaring_a_tool_that_does_not_exist_is_a_problem() -> None:
    """A typo is a write the step believes it declared and does not have."""
    (problem,) = _problems(["record_knowledge_notes"])

    assert "unknown write tool" in problem


def test_declaring_a_write_the_step_profile_does_not_advertise_is_a_problem() -> None:
    """Declaring a write the step profile does not advertise is a problem.

    `step_profile` intersects with the profile's surface, so such a write would silently do nothing.
    `property-lookup` advertises no knowledge-graph write.
    """
    (problem,) = _problems(["record_knowledge_note"], profile="property-lookup")

    assert "does not advertise" in problem
    assert "property-lookup" in problem


def test_declaring_a_real_write_the_profile_advertises_is_no_problem() -> None:
    """Declaring a real write the profile advertises is no problem.

    An in-process write on the default profile, and a connector tool `property-lookup` advertises.
    """
    assert _problems(["record_knowledge_note"]) == []
    assert _problems([_CONNECTOR_WRITE], profile="property-lookup") == []


def test_the_cli_gate_checks_a_step_profile_against_the_profiles_that_exist(
    tmp_path: Path,
) -> None:
    """`make template-validate` checks a step profile against the profiles that exist.

    `main` must load the profile files before snapshotting `registered_profile_names()`. Driven in a
    subprocess as the Makefile does, since the tests above load profiles themselves.
    """
    (tmp_path / "profile-probe.yaml").write_text(
        "summary: Probe.\n"
        "description: A step naming a shipped profile and declaring a write outside it.\n"
        "steps:\n"
        "  - id: brief\n"
        "    kind: agent\n"
        "    purpose: Probe.\n"
        "    profile: property-lookup\n"
        "    write_tools:\n"
        "      - record_knowledge_note\n"
        "    prompt: write it up\n",
        encoding="utf-8",
    )

    run = subprocess.run(
        [sys.executable, "-m", "chemclaw.cli.validate_templates"],
        capture_output=True,
        text=True,
        env={**os.environ, "CHEMCLAW_TEMPLATES_DIR": str(tmp_path)},
    )

    assert "names unknown profile" not in run.stdout, run.stdout + run.stderr
    assert "record_knowledge_note" in run.stdout, run.stdout + run.stderr
    assert "does not advertise" in run.stdout, run.stdout + run.stderr
    assert run.returncode == 1, run.stdout + run.stderr


# --- a capped step is not a finished one ---------------------------------------------------------
#
# Both caps end a turn by returning from `before_model`, after the tool node, so a capped step's
# last message is a `ToolMessage`. The step must answer with its own last text, not the tool body,
# and book a capped outcome rather than `answered`.


def _looping(turns: int, usage: dict[str, Any] | None = None) -> list[AIMessage]:
    """`turns` assistant messages that each say something and ask for another tool call.

    Prose on each turn means returning the tool body would be a choice, not the only option. `ls` is
    registered by `FilesystemMiddleware` on every agent, so no connector is needed.
    """
    return [
        AIMessage(
            content=f"partial {index}",
            tool_calls=[{"name": "ls", "args": {"path": "."}, "id": f"call-{index}"}],
            usage_metadata=usage,
        )
        for index in range(turns)
    ]


def _quiet_looping(turns: int) -> list[AIMessage]:
    """One assistant turn that says something, then `turns - 1` tool calls carrying no text.

    Here "the last `AIMessage`" and "the last assistant text" differ, as they do with real
    providers.
    """
    said = AIMessage(
        content="Here is what I found so far: CCO is ethanol.",
        tool_calls=[{"name": "ls", "args": {"path": "."}, "id": "call-0"}],
    )
    silent = [
        AIMessage(
            content="",
            tool_calls=[{"name": "ls", "args": {"path": "."}, "id": f"call-{index}"}],
        )
        for index in range(1, turns)
    ]
    return [said, *silent]


def test_a_capped_step_answers_with_the_last_text_rather_than_the_last_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capped step answers with the last text rather than the last message.

    The last `AIMessage` may be a content-less tool call, which would hand back `''`.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 3)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(monkeypatch, _step(), _quiet_looping(8))

    assert step.answer == "Here is what I found so far: CCO is ethanol."
    assert [row.outcome for row in step.costs] == ["loop_capped"]


def test_a_turn_that_said_nothing_does_not_answer_with_the_previous_turn_s_answer() -> None:
    """A turn that said nothing does not answer with the previous turn's answer.

    `result["messages"]` is the whole thread, so the walk back stops at this turn's user message.
    """
    thread = [
        HumanMessage(content="what is CCO?"),
        AIMessage(content="Ethanol."),
        HumanMessage(content="and its boiling point?"),
        AIMessage(content="", tool_calls=[{"name": "ls", "args": {}, "id": "c1"}]),
        ToolMessage(content="No files found", tool_call_id="c1"),
    ]

    assert answer_text({"messages": thread}) == ""
    assert answer_text({"messages": thread[:2]}) == "Ethanol."


def test_a_loop_capped_step_answers_with_prose_and_is_booked_as_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop-capped step answers with its own prose and is booked as capped."""
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 3)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(monkeypatch, _step(), _looping(8))

    assert "No files found" not in step.answer, (
        "the step answered with the tool's own output; a ToolMessage is not an assistant answer"
    )
    # The fourth call is the tool-less wrap-up the cap owes the step (`loop_cap.AnswerAtTheCap`);
    # its text is the answer, and its tool call is dropped rather than run.
    assert step.answer == "partial 3"
    assert [row.outcome for row in step.costs] == ["loop_capped"]


def test_a_spend_capped_step_is_booked_as_capped_rather_than_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same for the other cap, which ends the run from the hook immediately after it.

    Driven through the real activity rather than the hook, because the defect was that the caller
    never asked: `spend_capped(result)` was correct throughout and had no reader in `src/`.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 25)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 200)

    step = _drive(monkeypatch, _step(), _looping(8, {**_USAGE, "total_tokens": 120}))

    assert [row.outcome for row in step.costs] == ["spend_capped"]
    assert "No files found" not in step.answer


# --- degradation an `agent` step must report ------------------------------------------------------
#
# A step that ran with connector bundles unreachable, or was stopped by a cap, would otherwise
# return a string shaped exactly like a complete one, and the run's summary would read as clean.
# `unreachable` from `open_connector_specs` and the cap flags must reach the step's result.


def test_a_clean_step_carries_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control arm, first: nothing is added to an answer that is whole.

    Without this the three below are satisfied by a notice on *every* step, which would make the
    marker meaningless in exactly the way an always-on warning is.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 25)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(monkeypatch, _step(), ["five hazard flags; two are severe"])

    assert step.result.degraded is False
    assert step.result.notice() == ""
    assert step.result.step_value() == "five hazard flags; two are severe"
    assert run_summary("hazard-briefing", 1, {}) == "template 'hazard-briefing' completed 1 step(s)"


def test_a_step_whose_bundles_were_dark_says_so_where_the_next_step_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step whose bundles were unreachable says so where the next step reads.

    A template has no event stream to warn on, so the notice must ride on the value passed on.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 25)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(
        monkeypatch, _step(), ["five hazard flags; two are severe"], unreachable=["eln", "calc"]
    )

    assert step.result.unreachable == ["eln", "calc"]
    assert step.result.degraded is True
    # By name, because "2 unreachable" sends nobody anywhere.
    assert "calc" in step.result.notice() and "eln" in step.result.notice()
    value = step.result.step_value()
    assert value.startswith("[INCOMPLETE"), value
    # The answer itself is still there — a degraded answer is delivered, marked, not withheld.
    assert value.endswith("five hazard flags; two are severe")


def test_a_capped_step_hands_on_a_marked_partial_rather_than_a_bare_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comment above the connector call said this landed; it landed in the ledger only.

    "a truncated runaway booked `outcome="answered"` and handed the next step of the template a
    partial answer with nothing saying so" — the booking was fixed, the handing-on was not.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 3)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(monkeypatch, _step(), _looping(8))

    assert step.result.outcome == "loop_capped"
    assert step.result.degraded is True
    assert step.result.step_value().startswith("[INCOMPLETE")
    assert step.result.step_value().endswith("partial 3")  # the wrap-up call's text


def test_the_runs_record_states_which_step_ran_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's record states which step ran degraded.

    `find_past_jobs` and `get_durable_job_status` render only `summary`, so the degradation is in
    it, and `result["degraded"]` carries it machine-readably. `state` stays `completed`: the run did
    run to its end.
    """
    monkeypatch.setattr(settings, "harness_max_loop_iterations", 25)
    monkeypatch.setattr(settings, "agent_max_turn_billed_tokens", 0)

    step = _drive(monkeypatch, _step(), ["five hazard flags; two are severe"], unreachable=["eln"])
    degradations = {"brief": step.result.notice()}
    summary = run_summary("hazard-briefing", 2, degradations)

    assert summary == "template 'hazard-briefing' completed 2 step(s) — DEGRADED at brief"
    record = template_job_record(
        "wf-1",
        _template_run(),
        {"brief": step.result.step_value()},
        summary,
        degradations,
    )
    assert record.state == "completed"
    assert record.summary == summary
    assert record.result["degraded"] == degradations
    # A clean run's row is byte-identical to what it has always been.
    clean = template_job_record("wf-2", _template_run(), {"brief": "whole"}, "done")
    assert clean.result == {"steps": {"brief": "whole"}}


def _template_run() -> Any:
    """One `TemplateRunInput` — imported here because only this group needs the workflow's input."""
    from chemclaw.durable.template_job import TemplateRunInput
    from chemclaw.templates.manifest import Template

    return TemplateRunInput(
        template=Template.model_validate(
            {
                "name": "hazard-briefing",
                "summary": "Screen a molecule for hazards and write a brief.",
                "inputs": [{"name": "smiles", "type": "string", "description": "The molecule."}],
                "steps": [
                    {
                        "id": "brief",
                        "kind": "agent",
                        "purpose": "Turn the flags into something a chemist can act on.",
                        "prompt": "Write a short brief for ${inputs.smiles}.",
                    }
                ],
            }
        ),
        inputs={"smiles": "CCO"},
        requested_by="chemist-1",
    )


def test_the_step_still_admits_the_answer_an_old_worker_returns() -> None:
    """The step still admits the bare-string answer an old worker returns.

    During a rollout a new workflow may schedule this activity on an old worker; without the `| str`
    union the pydantic converter would fail the run. Checked through the converter's own adapter.
    `tests/test_templates.py` drives the sequencer half end to end.
    """
    from pydantic import TypeAdapter
    from temporalio.contrib.pydantic import pydantic_data_converter

    from chemclaw.durable import template_activities

    hints = typing.get_type_hints(template_activities.run_agent_step)
    adapter: TypeAdapter[Any] = TypeAdapter(hints["return"])

    assert adapter.validate_python("briefing text") == "briefing text"
    assert isinstance(adapter.validate_python({"answer": "x"}), AgentStepResult)
    # And the worker's converter is the pydantic one, which is what makes the adapter above the
    # right thing to have asked (`core/temporal_client.py`).
    assert pydantic_data_converter is not None


# --- the prompt a step is handed is bounded, because nothing else on this path bounds it ---------
#
# A `tool` step runs through `invoke_governed` without `bound_tool_results`, so the next step's
# prompt can interpolate an unbounded `${steps.<id>.result}`. Compaction cannot reclaim it either:
# a step is one `HumanMessage` with no history.


#: What the step's model was asked to answer, one entry per model call.
_SEEN: list[list[Any]] = []


def _model_prompt(monkeypatch: pytest.MonkeyPatch, prompt: str) -> str:
    """Drive the real activity on `prompt` and return the human text the model actually received.

    Recorded at the model, since `turn_input`, prompt assembly and the model node sit between the
    cut and the provider. Both `_generate` and `_stream` are patched, since which one runs is
    LangChain's choice. Patched on the class: a pydantic subclass could not take the script
    shorthand.
    """
    _SEEN.clear()
    for hook in ("_generate", "_stream"):
        original = getattr(ScriptedChatModel, hook)

        def recorded(
            self: Any, messages: list[Any], *args: Any, _original: Any = original, **kwargs: Any
        ) -> Any:
            _SEEN.append(list(messages))
            return _original(self, messages, *args, **kwargs)

        monkeypatch.setattr(ScriptedChatModel, hook, recorded)

    _drive(monkeypatch, _step(prompt=prompt, template="tautomer-resolution"), ["ok"])

    human = [
        message for request in _SEEN for message in request if isinstance(message, HumanMessage)
    ]
    assert human, "the step's own prompt never reached the model as a human message"
    return str(human[0].content)


def test_an_oversized_step_prompt_reaches_the_model_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized step prompt reaches the model bounded.

    The ceiling is `agent_max_tool_result_chars`, the one answer to how much text may reach a model
    in one blob.
    """
    ask = "Report the tautomer resolution of CCO."
    close = "Close by naming which downstream numbers this changes."
    ranking = '{"g": 0.01}, ' * 20_000
    prompt = f"{ask}\n\nRanking: {ranking}\n\n{close}"
    assert len(prompt) > settings.agent_max_tool_result_chars

    sent = _model_prompt(monkeypatch, prompt)

    assert len(sent) <= settings.agent_max_tool_result_chars
    # Head *and* tail, which is the property that makes cutting a prompt safe at all: a template
    # prompt is instructions, then data, then instructions, and the judgment the step exists for is
    # in the last sentences. A head-only cut would keep the ask and throw away the answer's shape.
    assert sent.startswith(ask)
    assert sent.rstrip().endswith(close)
    assert "characters removed from the middle" in sent


def test_the_cut_tells_the_step_something_it_can_actually_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cut tells the step something it can actually do.

    The step's model did not ask for this text, so advice to narrow its question would be wrong.
    """
    prompt = "Brief me.\n\n" + ("x" * settings.agent_max_tool_result_chars) + "\n\nBe brief."

    sent = _model_prompt(monkeypatch, prompt)

    assert STEP_REMEDY in sent
    assert TOOL_REMEDY not in sent
    # And it is named as this system's speech, which is the part a connector cannot forge: the
    # interpolated half of that prompt is a tool result, so an unmarked sentence inside it would be
    # asking to be believed on the strength of its own wording (`agent/tool_result_size._notice`).
    assert SYSTEM_SPEECH_MARK in sent


def test_a_prompt_inside_the_ceiling_is_handed_over_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt inside the ceiling is handed over untouched.

    Otherwise the tests above would pass with unconditional cutting.
    """
    sent = _model_prompt(monkeypatch, "brief me on CCO")

    assert sent == "brief me on CCO"
    assert "characters removed" not in sent


def test_the_counter_names_the_template_whose_prompt_was_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter names the template whose prompt was cut.

    Labelled by template rather than tool, because a truncated prompt is fixed in the template.
    """
    seen: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        METRICS,
        "increment",
        lambda name, value=1.0, labels=None: seen.append((name, labels)),
    )

    _model_prompt(monkeypatch, "a\n\n" + "x" * settings.agent_max_tool_result_chars + "\n\nb")

    assert ("chemclaw_template_prompt_truncated_total", {"template": "tautomer-resolution"}) in seen
