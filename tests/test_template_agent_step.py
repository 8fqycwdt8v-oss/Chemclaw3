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
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import pytest
from langchain_core.tools import tool as tool_decorator

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import advertised_tool_names
from chemclaw.durable.template_activities import AgentStepInput, StepIdentity, step_profile
from chemclaw.templates.manifest import AgentStep
from tests.fakes_langgraph import ScriptedChatModel

# A `calc` endpoint tool the manifest classifies `state_changing`. The whole point of testing with
# this one rather than an in-process write: it lives on the *other* side of the surface, which is
# the half a narrowing applied to only the builder's profile leaves wide open.
_CONNECTOR_WRITE = "compute_xtb_energy"


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


def _drive(
    monkeypatch: pytest.MonkeyPatch, step: AgentStepInput, script: list[Any]
) -> tuple[str, list[str], list[Any], list[str]]:
    """Run the real `run_agent_step` against a scripted model, and report what happened.

    Only two things are substituted, and neither is on the path under test:

    - `chemclaw.agent.langgraph_agent.build_chat_model`, the seam `tests/test_agent_team.py`
      documents as the one to use precisely because patching it runs the *production* wiring rather
      than a hand-assembled stand-in;
    - `open_connector_specs`, because no MCP server is running here — an unreachable connector
      contributes no tools at all, so a live-registry run would prove nothing about the connector
      half either way. The stand-in builds one tool per name each spec's allow-list *actually
      carries*, which is what makes this a test of the narrowing rather than of the transport.

    Returns the step's answer, the tool bodies that ran, the audit events, and every tool name the
    specs handed to `open_connector_specs` advertised.
    """
    from chemclaw.durable import template_activities

    calls: list[str] = []
    offered: list[str] = []
    sink = _Recorder()
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: sink)
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model",
        lambda *_a, **_k: ScriptedChatModel(script),
    )

    async def fake_open(_stack: AsyncExitStack, specs: Any) -> tuple[list[Any], list[str]]:
        names = [name for spec in specs for name in (spec.allowed_tools or [])]
        offered.extend(names)
        return [_stand_in(name, calls) for name in names], []

    monkeypatch.setattr(template_activities, "open_connector_specs", fake_open)
    answer = asyncio.run(template_activities.run_agent_step(step))
    return answer, calls, sink.events, offered


def _step(**overrides: Any) -> AgentStepInput:
    """One `agent` step input, defaulting to the read-only shape a template gets for free."""
    payload: dict[str, Any] = {
        "prompt": "brief me on CCO",
        "identity": StepIdentity(actor="chemist-1", roles=[], correlation_id="template-run-1"),
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
    2. the attempt is a GxP row with `outcome="error"` **saying why it was refused**. The outcome
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
    answer, calls, events, offered = _drive(
        monkeypatch,
        _step(),
        [{"name": _CONNECTOR_WRITE, "args": {"smiles": "CCO"}}, "no flags matched"],
    )

    assert calls == [], f"an undeclared write executed: {calls}"
    assert _CONNECTOR_WRITE not in offered, (
        "the step opened connectors still advertising the write — the profile is being resolved "
        f"twice, and only the builder's copy is narrowed; offered: {sorted(set(offered))}"
    )
    (refused,) = [e for e in events if e.tool == _CONNECTOR_WRITE]
    assert (refused.outcome, refused.actor) == ("error", "chemist-1"), refused
    assert "UndeclaredWriteRefusal" in refused.detail, refused.detail
    assert "not a valid tool" not in refused.detail, refused.detail
    assert answer == "no flags matched"


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
    answer, calls, events, offered = _drive(
        monkeypatch,
        _step(write_tools=[_CONNECTOR_WRITE]),
        [{"name": _CONNECTOR_WRITE, "args": {"smiles": "CCO"}}, "done"],
    )

    assert calls == [_CONNECTOR_WRITE]
    assert _CONNECTOR_WRITE in offered
    assert [e.outcome for e in events if e.tool == _CONNECTOR_WRITE] == ["ok"]
    assert answer == "done"


def test_a_read_tool_stays_reachable_without_any_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is read-only, not tool-less — a step that cannot look anything up is useless.

    `screen_hazards` is a `safety` endpoint tool the manifest classifies `read_only`, so it survives
    the subtraction with nothing declared.
    """
    answer, calls, _events, offered = _drive(
        monkeypatch,
        _step(),
        [{"name": "screen_hazards", "args": {"smiles": "CCO"}}, "no flags"],
    )

    assert "screen_hazards" in offered
    assert calls == ["screen_hazards"]
    assert answer == "no flags"


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
