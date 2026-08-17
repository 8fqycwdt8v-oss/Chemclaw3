"""The `agent` step's surface: ungated by the plan gate, and read-only unless the file says so.

A template is not plan-gated, and that is a decision rather than an omission: a template *is* the
pre-approved plan — a human-authored, git-committed, reviewed YAML file that nothing at run time can
produce — so asking an `agent` step to get its plan approved would be asking for approval of a plan
nobody wrote, in a session that does not exist. What the plan gate also did was bound what a model
improvising inside one step could reach, and that half is kept by narrowing the step's surface
instead: every side-effecting tool the step did not declare is removed from the agent before it is
built.

**The tests here are about the half that is easy to get wrong.** The narrowing is a subtraction
applied to a profile, and the profile used to be resolved *twice* — the raw name to
`connector_specs`, a modified copy to the builder. Narrowing only the builder's copy looks correct
in every in-process assertion and leaves the entire connector surface bound, including
`compute_xtb_energy`, which `connectors/calc/connector.yaml` classifies `state_changing` and which
is the exact tool `agent/authz.side_effecting_tools`'s own docstring names as the one a set built
from in-process names would have missed. So the headline test drives the real activity, the real
graph and the real `connector_specs`, and asserts on the connector half.

**The second group is about the half that was not there at all.** An `agent` step is a model turn,
and every instrument the chat path points at a model turn was absent from this one: the ambient
session (so every audit row a template wrote booked `session_id=""`), the token counters, and the
`turn_costs` ledger. Measured before the fix on this very activity, with a scripted model reporting
120 tokens per call: `chemclaw_tokens_total` 0.0 before and 0.0 after. Those tests drive the real
activity too, for the same reason as the first group — the defect was that the *caller* did none of
it, so a test of the arithmetic would have passed throughout.
"""

import asyncio
import contextlib
from contextlib import AsyncExitStack
from typing import Any, NamedTuple

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as tool_decorator
from temporalio.testing import ActivityEnvironment

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import advertised_tool_names
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.core.metrics import METRICS
from chemclaw.durable.template_activities import (
    AgentStepInput,
    StepIdentity,
    ToolStepInput,
    step_profile,
)
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

    It records its own name when its **body** runs, which is the assertion that matters. "The model
    was not offered the tool" is not the claim under test — the claim is that the write did not
    happen, and only the body can testify to that.
    """

    @tool_decorator(name_or_callable=name, description=f"stand-in for {name}")
    async def _fake(smiles: str) -> str:
        calls.append(name)
        return f"{name} ran"

    return _fake


def _scripted(script: list[Any] | ScriptedChatModel) -> ScriptedChatModel:
    """The step's model: the shared fake's script shorthand, or ready-made messages.

    `ScriptedChatModel`'s shorthand (a string, or a `{"name", "args"}` mapping) has nowhere to put
    `usage_metadata`, and the metering tests below are *about* the usage a provider reports — so a
    script written as `AIMessage`s is handed to the fake's own `messages` iterator instead. One
    function, so every test in this file drives the same model whichever shape it wrote.
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


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    step: AgentStepInput,
    script: list[Any] | ScriptedChatModel,
) -> _Step:
    """Run the real `run_agent_step` against a scripted model, and report what happened.

    Only three things are substituted, and none is on the path under test:

    - `chemclaw.agent.llm_provider.build_chat_model`, the seam to patch precisely because doing
      so runs the *production* wiring rather than a hand-assembled stand-in;
    - `open_connector_specs`, because no MCP server is running here — an unreachable connector
      contributes no tools at all, so a live-registry run would prove nothing about the connector
      half either way. The stand-in builds one tool per name each spec's allow-list *actually
      carries*, which is what makes this a test of the narrowing rather than of the transport;
    - the turn-cost sink, so the ledger row is observable without a database. `record_turn_cost`
      writes from a task it deliberately does not await (`agent/turn_cost.py` explains why), so
      the run yields once afterwards to let that task reach a recorder that never blocks.

    Returns the step's answer, the tool bodies that ran, the audit events, every tool name the
    specs handed to `open_connector_specs` advertised, and the cost rows booked.
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
        return [_stand_in(name, calls) for name in names], []

    monkeypatch.setattr(template_activities, "open_connector_specs", fake_open)

    async def _run() -> str:
        answer = await template_activities.run_agent_step(step)
        # One scheduling round is enough for a recorder that never awaits anything real; the point
        # is only that the cost task gets to run before the loop `asyncio.run` closes it.
        await asyncio.sleep(0)
        return answer

    return _Step(asyncio.run(_run()), calls, sink.events, offered, costs)


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
    """The whole decision, driven end to end through the real activity and the real graph.

    Four claims, and the third is the one that needed the double resolution fixed:

    1. the write's **body never ran** — not that it was hidden from the model, that it did not
       happen;
    2. the attempt is an audit row with `outcome="error"` **saying why it was refused**. The outcome
       alone proves less than it looks: `ToolNode` *returns* its invalid-name message from inside
       the wrapper chain, so `returned_failure` books an error row either way, and with the refusal
       middleware deleted this row reads "compute_xtb_energy is not a valid tool, try one of
       [list_attachments, read_attachment, …]" — the library's guess at a typo, with the agent's
       whole remaining inventory in the field an auditor reads as what happened. So the `detail` is
       asserted, not just the outcome;
    3. the connector specs the step opened never advertised it, which is the half that stayed wide
       open while the profile was resolved twice;
    4. the turn **still answers**. A refusal that ended the run would have turned a narrowing into
       an outage, and the model is handed the refusal as this call's result rather than as an error
       worth retrying.
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
    assert (refused.outcome, refused.actor) == ("error", "chemist-1"), refused
    assert "UndeclaredWriteRefusal" in refused.detail, refused.detail
    assert "not a valid tool" not in refused.detail, refused.detail
    assert run.answer == "no flags matched"


def test_the_model_reads_a_refusal_rather_than_a_retryable_error() -> None:
    """What the *model* is handed, which is the only thing the refusal middleware changes.

    Structure already stops the call; this is about the signal that comes back. LangGraph's own
    answer to an unbound name is `ToolMessage(status="error")` carrying "is not a valid tool, try
    one of [...]" — `is_error` on the wire, an explicit invitation to retry, and the agent's whole
    inventory in the transcript — for a tool that was withheld deliberately rather than mistyped.
    `_refusal_message` records why `status="error"` is the wrong signal for a decision; this pins
    that an undeclared write gets the right one.
    """
    from chemclaw.agent.audit import NullAuditSink
    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.state import turn_config, turn_input

    graph = build_langgraph_agent(
        ScriptedChatModel(
            [{"name": "propose_knowledge_note", "args": {"title": "x"}}, "could not do that"]
        ),
        profile=step_profile(None, []),
        audit_sink=NullAuditSink(),
    )
    result = asyncio.run(graph.ainvoke(turn_input("write it up"), turn_config()))

    (message,) = [m for m in result["messages"] if getattr(m, "type", "") == "tool"]
    assert message.status != "error", "a deliberate refusal must not reach the model as is_error"
    assert message.text.startswith("Refused: propose_knowledge_note changes stored data"), (
        message.text
    )
    assert "try one of" not in message.text


def test_a_declared_write_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, or the narrowing would be a ban rather than a declaration.

    Same step, same tool, same script — the only difference is one line in the template — so this
    also pins that the connector *spec* is what carries the difference: the tool is offered here and
    absent above, from the same registry.
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
    """A two-call turn — a tool call then an answer — each reporting the usage a provider reports.

    Two calls rather than one, because the sum is the thing: a metering that read only the final
    message would pass every single-call test and silently under-report every real step, which
    makes tool calls (the reason an `agent` step exists) free.
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
    """Every audit row a template ever wrote booked an empty session id.

    `set_current_session_id` is what `agent/audit.py` reads (`get_current_session_id() or ""`), and
    the chat path stamps it on every turn. The step activities stamped the *actor* and never the
    session — the id was in `TemplateRunInput` and used only for the completion push-back — so the
    trail could answer "who" and "which run" and could not answer "which conversation". Measured on
    this activity before the fix: `session_id=''`.

    Asserted on a tool row rather than on a synthetic one, because that is the row an auditor reads,
    and it is written from inside the graph — so it also pins that the stamp survives the whole
    depth of the call, not just the activity's own frame.
    """
    run = _drive(monkeypatch, _step(), _metered_script())

    (event,) = [e for e in run.events if e.tool == "screen_hazards"]
    assert (event.actor, event.session_id) == ("chemist-1", "s-tmpl"), event


def test_the_steps_tokens_reach_the_counters_the_deployment_bills_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass itself: a template's spend was invisible to every token counter there is.

    Measured before the fix, driving this same activity with this same script:
    `chemclaw_tokens_total` read 0.0 before and 0.0 after, and so did the four split counters. A
    template was therefore a way to spend model tokens that no dashboard, no alert and no cost
    review would ever see.

    Asserted as a *delta* rather than an absolute, because `METRICS` is a process-wide registry
    that other tests in the same session also write to. The split counters are checked beside the
    total because they are priced separately (`agent/turn_usage.py`), and a metering that published
    one number four times would satisfy a total-only assertion.
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
    """A model that serves `paid` calls and then fails, counting what the provider actually billed.

    The call count is kept provider-side because a failing turn has no message list to read: the
    whole defect being guarded is that spend was totalled from `result["messages"]`, which does not
    exist when `ainvoke` raises.
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
    """A step that broke after two model calls still spent them.

    Measured before the fix: a provider error after two paid calls wrote `turn_costs (0, 0)` and
    moved `chemclaw_tokens_total` by 0.0, because the sum was taken from `result["messages"]` after
    `ainvoke` returned — and an exception skips that. The `finally` then booked an all-zero row,
    which is worse than no row: it asserts the step cost nothing. The runaway case is the same
    defect and is the one the metering was added to make visible, so the accumulation had to move
    to a callback that fires as each call ends.
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
    """The guard on the other side: the callback must replace the post-hoc sum, not join it.

    Keeping both accumulation paths is the obvious way to make the failure test pass, and it
    double-bills every successful step — 480 for a two-call turn. Asserted as `calls x per-call
    usage` so neither a doubled nor a dropped call satisfies it.
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
    """The other instrument, and the one the counters cannot replace.

    `chemclaw_tokens_total` is labelled `profile` and capped at 64 label series by construction, so
    it can say what the deployment spends and never what one chemist spent — that is what
    `turn_costs` is for (`agent/turn_cost.py`). A template step wrote no row at all, so a procedure
    launched from a conversation was spend with no owner.

    The row's key is the run's correlation id **plus the step id**: `turn_costs` upserts on the
    correlation id, and every step of a run shares the run's, so a bare run id would make a
    multi-`agent`-step template overwrite its own earlier steps and report the last one's spend as
    the whole run's.
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
    """The subtraction itself, over the live registry rather than a fixture.

    Asserted against `side_effecting_tools()` — the same set the dry-run guard and the plan gate
    decide with — because a second classification is the second source of truth this tree forbids,
    and it would be wrong in the same direction each time: only a bundle's own manifest knows that
    `compute_xtb_energy` spends compute while `resolve_compound` is a lookup.
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
    """The one link the activity-level tests cannot see: what the workflow actually sends.

    Every test above builds an `AgentStepInput` by hand, so a `_run_step` that dropped
    `write_tools` on the floor would leave all of them green while every real run silently ran
    read-only — the failure mode that is invisible from either end alone.

    Substituting the module's `workflow` handle rather than driving a server, the same way
    `tests/test_templates.py` does for the retry policies: the real workflow API refuses to run
    outside a workflow event loop, and the function under test is the real, unmodified `_run_step`.
    """
    import types
    from datetime import timedelta

    from chemclaw.durable import template_job

    sent: list[Any] = []

    async def execute_activity(_activity: Any, payload: Any, **_kwargs: Any) -> str:
        sent.append(payload)
        return "ok"

    step = AgentStep(id="brief", prompt="write it up", write_tools=["propose_knowledge_note"])
    identity = StepIdentity(actor="chemist-1", roles=[], correlation_id="run-1")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            template_job, "workflow", types.SimpleNamespace(execute_activity=execute_activity)
        )
        asyncio.run(
            template_job.TemplateWorkflow()._run_step(step, {}, identity, timedelta(seconds=60))
        )

    (payload,) = sent
    assert payload.write_tools == ["propose_knowledge_note"]


def test_every_dispatched_step_carries_a_heartbeat_timeout() -> None:
    """A step that never says anything is indistinguishable from a worker that died.

    Both dispatched activities now beat while they wait (`durable/heartbeat.beating`), and a beat
    nobody is listening for is not a liveness signal — Temporal only reacts to one if the activity
    was scheduled with a `heartbeat_timeout`. There was none on any step, so `start_to_close` was
    the sole signal: a worker evicted one minute into a 900 s `agent` step left the run waiting out
    the whole remaining budget before retrying an attempt that had been dead the entire time.

    Both kinds in one test, because the failure mode is a step kind arriving without the option —
    which is exactly how the activities themselves once shipped unregistered
    (`test_every_template_step_activity_is_registered_on_a_worker`).

    Substituting the module's `workflow` handle rather than driving a server, like its sibling
    above: the real workflow API refuses to run outside a workflow event loop, and the function
    under test is the real, unmodified `_run_step`.
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
                template_job.TemplateWorkflow()._run_step(step, {}, identity, timedelta(seconds=60))
            )

    expected = timedelta(seconds=settings.template_step_heartbeat_timeout_seconds)
    assert [o.get("heartbeat_timeout") for o in options] == [expected, expected], options
    # The per-attempt budget stays beside it: a heartbeat bounds silence, not the work.
    assert [o.get("start_to_close_timeout") for o in options] == [
        timedelta(seconds=60),
        timedelta(seconds=60),
    ]


# --- and the beat the option is listening for ----------------------------------------------------


# A `safety` endpoint tool the manifest classifies `read_only`, so it survives an `agent` step's
# narrowing with nothing declared and passes a `tool` step's authorization as itself. The stand-in
# below borrows the name because what is under test is the *wrapper around the wait*, not which
# tool is waiting.
_SLOW_TOOL = "screen_hazards"
# What the two activities are given as their heartbeat timeout while this test drives them.
# `durable/heartbeat.beating` derives its beat interval from this value — a quarter of it, floored
# at one second — so four is the smallest number that still exercises the shipped arithmetic rather
# than a special case: four seconds in, a beat at one.
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
        """Block until this activity has been heartbeat for, or until the deadline lapses.

        Waiting *for the beat* rather than sleeping a fixed span is what makes this both quick and
        not a race: the healthy run ends the instant the timer fires, and the broken one is not
        losing a bet against a sleep on a loaded machine. The deadline is suppressed rather than
        raised so the failure is the assertion below — nothing beat — rather than a `TimeoutError`
        surfacing out of the middle of somebody else's activity.
        """
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._first.wait(), _BEAT_DEADLINE_SECONDS)


def _beats_of(monkeypatch: pytest.MonkeyPatch, activity: Any, payload: Any) -> _Beats:
    """Run one real template step activity, in an activity context, over one slow tool.

    The slowness is the point and it is put where a real step's slowness is: in the tool. Both
    activities wrap their whole wait in `beating`, so a tool that does not return until it has been
    heartbeat for is enough to observe the timer from outside — and `ActivityEnvironment` is what
    makes `activity.heartbeat` legal here at all, since it raises outside an activity context.

    Everything substituted is what `_drive` substitutes and for the same reasons (no MCP server, no
    provider credential, no database), plus the heartbeat timeout, so a beat is one second away
    rather than fifteen.
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
    """The half an audit deleted while the whole suite stayed green.

    `test_every_dispatched_step_carries_a_heartbeat_timeout` pins that the *workflow* schedules both
    steps with a `heartbeat_timeout`, and nothing else asserted that anything ever beats. So
    removing the `beating(...)` wrapper from both activities — the whole liveness mechanism — left
    every test in this file and its sibling passing. A heartbeat timeout is not a safety net on its
    own: it is a **deadline**, and the two changes together are what make a missing beat fatal
    rather than merely undetected. With 60 s configured and no beat, Temporal now kills any step
    that runs longer than a minute, which is every step worth dispatching to a worker.

    So this drives the real activities and watches for the beat itself, through
    `ActivityEnvironment.on_heartbeat` — the environment's own recording, not a spy on our wrapper,
    so it is `activity.heartbeat` actually being called that is observed and not our own idea of it.
    Both kinds, for the same reason the option test takes both: the failure mode is a step kind
    arriving without it.
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
    from chemclaw.agent.profile_discovery import load_profiles
    from chemclaw.cli.validate_templates import _available_tools, _step_problems
    from chemclaw.templates.manifest import Template

    # The file profiles are registered by the validator's entry point, not by `_step_problems` —
    # so a test calling the inner function has to do what the outer one does, or every named
    # profile reads as unknown.
    load_profiles()

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
    _available_tools()  # the in-process registry is an import side effect; see the validator
    return _step_problems(template)


def test_declaring_a_read_tool_as_a_write_is_a_problem() -> None:
    """The check that keeps the list from drifting into a general allow-list.

    A read tool is reachable with no declaration at all, so naming one grants nothing — and
    accepting it would let the list grow into an allow-list for the whole surface wearing a
    write-list's name, which is how a narrowing gets widened by people writing what looks like
    documentation.
    """
    (problem,) = _problems(["screen_hazards"])

    assert "changes nothing" in problem
    assert "screen_hazards" in problem


def test_declaring_a_tool_that_does_not_exist_is_a_problem() -> None:
    """A typo is a write the step believes it declared and does not have."""
    (problem,) = _problems(["propose_knowledge_notes"])

    assert "unknown write tool" in problem


def test_declaring_a_write_the_step_profile_does_not_advertise_is_a_problem() -> None:
    """A step cannot gain a tool its profile never had, and the file must say so out loud.

    `step_profile` intersects the declaration with the profile's advertised surface — attenuation
    only, the rule `agent/profiles.py` states — so a write outside it is accepted by YAML and
    silently nothing at run time. `property-lookup` is a shipped profile that advertises no
    knowledge-graph write.
    """
    (problem,) = _problems(["propose_knowledge_note"], profile="property-lookup")

    assert "does not advertise" in problem
    assert "property-lookup" in problem


def test_declaring_a_real_write_the_profile_advertises_is_no_problem() -> None:
    """The gate has to let the correct declaration through, or it is not a gate.

    Both halves, because a check that rejects everything passes every failure test above while
    making the feature unusable: an in-process write on the default profile, and a connector
    endpoint tool the `property-lookup` profile does advertise.
    """
    assert _problems(["propose_knowledge_note"]) == []
    assert _problems([_CONNECTOR_WRITE], profile="property-lookup") == []
