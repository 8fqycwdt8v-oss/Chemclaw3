"""Step templates: the contract, the substitution, and the run.

A template promises a fixed order and a reproducible run, so the tests guard what could break it:

- a reference that does not resolve stops the template from starting, rather than producing
  `None` halfway through a durable run;
- substitution preserves types, so a tool wanting a list does not receive its `repr`;
- the definition is pinned into the run, so editing a file cannot change what is executing (a
  correctness bug and a Temporal replay violation).

The end-to-end run needs a Temporal server and skips offline; the rest always runs.
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool as tool_decorator
from pydantic import ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from chemclaw.agent.template_surface import run_ceiling_problems
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.turn_signals import JobSignal
from chemclaw.templates import registry
from chemclaw.templates.manifest import AgentStep, Template
from chemclaw.templates.registry import (
    TemplateError,
    build_template_tool,
    discovered,
    run_workflow_id,
    tool_name,
)
from chemclaw.templates.resolve import UnresolvedReference, resolve
from chemclaw.templates.schedule import dependencies, schedule
from tests.signals import collect_signals

_MINIMAL = {
    "summary": "Do the thing.",
    "steps": [{"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": {}}],
}


def _template(**overrides: Any) -> Template:
    """Build a valid template with `overrides` applied."""
    payload: dict[str, Any] = {"name": "probe", **_MINIMAL}
    payload.update(overrides)
    return Template.model_validate(payload)


# --- the contract ---------------------------------------------------------------------


def test_a_step_may_only_reference_earlier_steps() -> None:
    """A forward reference is not a timing bug to debug at run time — it can never work."""
    with pytest.raises(ValidationError, match="not the result of an earlier step"):
        _template(
            steps=[
                {
                    "id": "first",
                    "kind": "agent",
                    "prompt": "use ${steps.second.result}",
                },
                {"id": "second", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
            ]
        )


def test_a_reference_to_an_undeclared_input_is_refused() -> None:
    """The failure this prevents is the expensive one: a null silently entering a calculation."""
    with pytest.raises(ValidationError, match="references unknown 'inputs.missing'"):
        _template(
            inputs=[{"name": "smiles", "type": "string", "description": "the molecule"}],
            steps=[
                {
                    "id": "one",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": "${inputs.missing}"},
                }
            ],
        )


def test_a_reference_nested_inside_arguments_is_still_checked() -> None:
    """Arguments are arbitrary JSON, so a check of the top level alone would miss most of them."""
    with pytest.raises(ValidationError, match="inputs.missing"):
        _template(
            steps=[
                {
                    "id": "one",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": [{"deep": "${inputs.missing}"}]},
                }
            ]
        )


@pytest.mark.parametrize(
    "bad",
    [
        "${step.one.result}",  # the manifest module's own docstring example
        "${steps.one.output}",
        "${inputs.smiles.canonical}",
        "${input.smiles}",
        "${steps.One.result}",
        "${ inputs.smiles }",
    ],
)
def test_a_malformed_reference_is_refused_rather_than_passed_through(bad: str) -> None:
    """A malformed reference is refused rather than passed through as literal text.

    A span `_REFERENCE` cannot match is invisible to every rule built on `references()`, and
    `make template-validate` checks argument keys, not values.
    """
    with pytest.raises(ValidationError, match="malformed reference"):
        _template(
            inputs=[{"name": "smiles", "type": "string", "description": "the molecule"}],
            steps=[
                {"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
                {
                    "id": "two",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": bad},
                },
            ],
        )


def test_a_malformed_reference_in_an_agent_prompt_is_refused() -> None:
    """The worst landing site: a prompt is prose, so a literal `${…}` is invisible to the model."""
    with pytest.raises(ValidationError, match="malformed reference"):
        _template(
            steps=[
                {"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
                {"id": "two", "kind": "agent", "prompt": "summarize ${steps.one.output}"},
            ]
        )


def test_a_malformed_reference_nested_inside_arguments_is_still_refused() -> None:
    """Same reach as the resolution check — both walk the whole argument tree, once."""
    with pytest.raises(ValidationError, match="malformed reference"):
        _template(
            steps=[
                {
                    "id": "one",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": [{"deep": "${input.smiles}"}]},
                }
            ]
        )


def test_duplicate_step_ids_are_refused() -> None:
    """Two steps with one id makes `${steps.<id>.result}` ambiguous."""
    with pytest.raises(ValidationError, match="duplicate step"):
        _template(
            steps=[
                {"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
                {"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
            ]
        )


def test_an_unknown_step_kind_is_refused() -> None:
    """The discriminated union fails loud rather than falling back to a default kind."""
    with pytest.raises(ValidationError):
        _template(steps=[{"id": "one", "kind": "telepathy", "tool": "x"}])


def test_a_template_needs_at_least_one_step() -> None:
    """An empty procedure is not a procedure."""
    with pytest.raises(ValidationError):
        _template(steps=[])


# --- substitution ---------------------------------------------------------------------


def test_a_whole_string_reference_preserves_the_value_type() -> None:
    """The distinction that keeps a list a list: a tool wanting `list[str]` must not get a repr."""
    scope = {"inputs.smiles": "CCO", "steps.hits.result": [{"id": "a", "score": 0.9}]}
    assert resolve("${inputs.smiles}", scope) == "CCO"
    assert resolve("${steps.hits.result}", scope) == [{"id": "a", "score": 0.9}]
    assert resolve({"smiles": ["${inputs.smiles}"]}, scope) == {"smiles": ["CCO"]}


def test_an_embedded_reference_interpolates_readable_text() -> None:
    """A prompt needs text, and JSON beats a Python repr the model has to guess at."""
    scope = {"steps.hits.result": {"flags": ["azide"]}}
    assert resolve("Flags: ${steps.hits.result}", scope) == 'Flags: {"flags": ["azide"]}'


def test_a_reference_with_trailing_text_is_not_a_whole_string_match() -> None:
    """`${inputs.smiles} plus buffer` interpolates rather than dropping " plus buffer".

    `_WHOLE`'s trailing `$` keeps an embedded reference from matching as a whole-string one.
    """
    scope = {"inputs.smiles": "CCO"}
    assert resolve("${inputs.smiles} plus buffer", scope) == "CCO plus buffer"


def test_a_reference_with_leading_text_is_not_a_whole_string_match() -> None:
    """Text before a reference survives too.

    This does not pin `_WHOLE`'s leading `^`: `re.match` anchors at position 0 anyway, so removing
    it is an equivalent mutant. The trailing `$` is pinned by the test above.
    """
    scope = {"inputs.smiles": "CCO"}
    assert resolve("solvent: ${inputs.smiles}", scope) == "solvent: CCO"


def test_an_unresolved_reference_raises_rather_than_yielding_empty() -> None:
    """Reaching this at run time means something is wrong beyond a typo — so it must be loud."""
    with pytest.raises(UnresolvedReference, match="steps.nope.result"):
        resolve("${steps.nope.result}", {})


def test_non_reference_values_pass_through_untouched() -> None:
    """Substitution must not mangle ordinary arguments — including a lone `$`."""
    scope: dict[str, Any] = {}
    assert resolve({"n": 3, "flag": True, "text": "costs $5"}, scope) == {
        "n": 3,
        "flag": True,
        "text": "costs $5",
    }


# --- the generated tool ---------------------------------------------------------------


def test_the_generated_tool_is_named_and_documented() -> None:
    """`run_<name>`, prefixed so a template cannot shadow a tool or a job — one namespace."""
    template = _template(
        inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}]
    )
    tool = build_template_tool(template)
    assert tool.__name__ == "run_probe"
    doc = tool.__doc__ or ""
    assert doc.startswith("Do the thing.")
    assert "smiles: The molecule." in doc
    assert "get_durable_job_status" in doc
    schema = tool.__annotations__["params"].model_json_schema()
    assert schema["properties"]["smiles"]["type"] == "string"


def test_a_hyphenated_template_name_becomes_a_valid_tool_name() -> None:
    """A tool name is an identifier the model calls and `tool_role_gates` keys on — not a hyphen."""
    assert tool_name(_template(name="hazard-briefing")) == "run_hazard_briefing"


class _FakeClient:
    """A Temporal client that records the start it was asked for instead of making one."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, _run: Any, arg: Any, **kwargs: Any) -> Any:
        self.calls.append({"input": arg, **kwargs})
        return type("Handle", (), {"id": kwargs["id"]})()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Point the launcher at a recording client, and give the turn an actor to attribute to."""
    fake = _FakeClient()

    async def connect() -> _FakeClient:
        return fake

    monkeypatch.setattr("chemclaw.templates.registry.connect", connect)
    monkeypatch.setattr("chemclaw.templates.registry.require_actor", lambda: "chemist@lab")
    return fake


def test_launching_accepts_the_raw_json_object_the_framework_hands_it(client: _FakeClient) -> None:
    """Launching accepts the raw JSON object the framework hands the tool body.

    The body receives a decoded `dict`, not the params model, and only calling the tool shows that.
    """
    tool = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    run_id = asyncio.run(tool(params={"smiles": "CCO"}))
    (call,) = client.calls
    assert call["input"].inputs == {"smiles": "CCO"}
    assert run_id == call["id"]


def test_launching_validates_rather_than_passing_the_object_through(client: _FakeClient) -> None:
    """Launching validates the dict rather than passing it through.

    The declared types are the contract. Validation lives in `start_template_run`, shared by both
    launchers, and the refusal names the declared inputs; its text is asserted.
    """
    tool = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    with pytest.raises(TemplateError) as raised:
        asyncio.run(tool(params={"wrong_field": "CCO"}))
    assert "smiles" in str(raised.value)
    assert "['smiles']" in str(raised.value), "the refusal has to name what the template declares"
    assert not client.calls, "nothing may be queued for a launch that does not type-check"


def test_launching_survives_the_frameworks_own_invocation_path(client: _FakeClient) -> None:
    """Launching survives the framework's own invocation path, `StructuredTool.ainvoke`."""
    from langchain_core.tools import StructuredTool

    fn = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    invocable = StructuredTool.from_function(
        coroutine=fn, name=fn.__name__, description="Run the probe template."
    )
    asyncio.run(invocable.ainvoke({"params": {"smiles": "CCO"}}))
    (call,) = client.calls
    assert call["input"].inputs == {"smiles": "CCO"}


def test_identical_inputs_produce_the_same_run_id() -> None:
    """The idempotency key: re-running the same procedure on the same input must not pay twice."""
    template = _template()
    assert run_workflow_id(template, {"smiles": "CCO"}) == run_workflow_id(
        template, {"smiles": "CCO"}
    )
    assert run_workflow_id(template, {"smiles": "CCO"}) != run_workflow_id(
        template, {"smiles": "CCC"}
    )


# --- the shipped template -------------------------------------------------------------


def test_the_shipped_template_is_valid_and_ordered() -> None:
    """`hazard-briefing` exists, screens before it writes, and its brief cites both earlier steps.

    The ordering *is* the feature — an agent might reasonably skip a screen it thought unnecessary,
    which for a safety brief is exactly the judgment nobody wants delegated.
    """
    template = discovered()["hazard-briefing"]
    assert [step.id for step in template.steps] == ["hazards", "precedent", "brief"]
    brief = template.steps[-1]
    assert isinstance(brief, AgentStep)
    assert "${steps.hazards.result}" in brief.prompt
    assert "${steps.precedent.result}" in brief.prompt


def test_a_broken_template_file_fails_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A template that could never run is a CI failure, not a run-time surprise."""
    (tmp_path / "broken.yaml").write_text(
        "summary: x\nsteps:\n  - {id: one, kind: agent, prompt: 'use ${steps.later.result}'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
    with pytest.raises(TemplateError, match="invalid template"):
        discovered()


def test_the_name_lives_in_the_filename_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for a profile: two sources of truth for one identity is drift waiting to happen."""
    (tmp_path / "named.yaml").write_text(
        "name: something-else\nsummary: x\nsteps:\n  - {id: one, kind: agent, prompt: hi}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
    with pytest.raises(TemplateError, match="name is its filename"):
        discovered()


def test_two_templates_generating_one_tool_name_fail_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two templates generating one tool name fail discovery.

    `tool_name` folds a hyphen to an underscore; otherwise `register_tool` would raise on every turn
    with a message naming neither file.
    """
    body = "summary: x\nsteps:\n  - {id: one, kind: agent, prompt: hi}\n"
    (tmp_path / "probe-x.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "probe_x.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))

    with pytest.raises(TemplateError, match="generates tool 'run_probe_x'"):
        discovered()


def test_the_gate_reports_the_tool_name_collision_rather_than_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And `make template-validate` is where a human sees it — it printed the green line before."""
    from chemclaw.cli.validate_templates import validate_templates

    body = "summary: x\nsteps:\n  - {id: one, kind: agent, prompt: hi}\n"
    (tmp_path / "probe-x.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "probe_x.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))

    problems = validate_templates()

    assert any("run_probe_x" in problem for problem in problems)


def test_the_validator_catches_a_step_naming_a_tool_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's reason to exist: a pinned procedure must not fail on step four in production."""
    from chemclaw.cli.validate_templates import validate_templates

    (tmp_path / "ghost.yaml").write_text(
        "summary: x\nsteps:\n  - {id: one, kind: tool, tool: no_such_tool}\n", encoding="utf-8"
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
    problems = validate_templates()
    assert any("unknown tool 'no_such_tool'" in problem for problem in problems)


def test_the_validator_catches_arguments_the_named_tool_does_not_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator catches arguments the named tool does not take.

    A template that validates and fails at its first live step is what the gate exists to prevent.
    Written on `predict_pka`, whose implementation is in this tree; the unresolvable case has its
    own test below.
    """
    from chemclaw.cli.validate_templates import validate_templates

    (tmp_path / "wrongargs.yaml").write_text(
        "summary: x\nsteps:\n"
        "  - id: one\n"
        "    kind: tool\n"
        "    tool: predict_pka\n"
        "    arguments:\n"
        "      smilez: CCO\n"
        "      nonexistent_arg: 42\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
    problems = validate_templates()
    assert any("does not take" in p and "nonexistent_arg" in p for p in problems), problems
    # And the other direction: dropping a required argument is the same class of run-time failure.
    assert any("omits required argument(s) ['smiles']" in p for p in problems), problems


def test_a_shipped_template_whose_arguments_cannot_be_checked_says_so() -> None:
    """A shipped template whose arguments cannot be checked says so by name.

    Bundles declared here but served by `Chemclaw3-mcp` have no local signatures, so their steps are
    name-checked only; `make live-template-args` checks them against a running server. Asserted on
    the real shipped set, so moving another bundle out changes the reported set visibly.
    """
    from chemclaw.cli.validate_templates import unchecked_arguments

    assert unchecked_arguments() == {
        "bond-strength-survey": ["enumerate_bond_cleavages"],
        "degradant-triage": ["enumerate_degradants", "screen_hazards"],
        "hazard-briefing": ["screen_hazards"],
        "microspecies-profile": ["enumerate_protonation_states"],
        "scale-up-thermal-envelope": [
            "adiabatic_temperature_rise",
            "mtsr",
            "stoessel_criticality_class",
        ],
        "stereoisomer-ranking": ["enumerate_stereoisomers"],
        "substitution-series": ["enumerate_substitutions"],
        "tautomer-resolution": ["enumerate_tautomers"],
    }


def test_the_validator_accepts_a_correct_tool_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator accepts a correct tool step.

    A gate that rejects correct input gets switched off. `top_k` is optional and omitted, so
    "required" must come from the signature's defaults.
    """
    from chemclaw.cli.validate_templates import validate_templates

    (tmp_path / "goodargs.yaml").write_text(
        "summary: x\nsteps:\n"
        "  - id: one\n"
        "    kind: tool\n"
        "    tool: similar_molecules\n"
        "    arguments:\n"
        "      smiles: 'CCO'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
    assert validate_templates() == []


def test_the_argument_check_covers_the_same_tools_whatever_the_call_order() -> None:
    """The argument check covers the same tools whatever the call order.

    `resolvable_signatures()` reads `registered_tools()`, which is populated by importing the agent
    package, so it must not rely on another function having imported it first. Run in a subprocess,
    since this session's registry cannot be emptied.
    """
    probe = (
        "from chemclaw.agent.template_surface import available_tools, resolvable_signatures\n"
        "first = set(resolvable_signatures())\n"
        "available_tools()\n"
        "print('SAME' if first == set(resolvable_signatures()) else 'DIFFERENT', len(first))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    verdict, count = result.stdout.split()[-2:]
    assert verdict == "SAME", result.stdout + result.stderr
    assert int(count) > 40, f"only {count} signatures resolved in a fresh interpreter"


def test_a_bundle_that_cannot_be_imported_stops_the_template_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle that cannot be imported stops the template gate.

    A broken import must not look like a bundle without a server module; the import is one shared
    raising function, so this gate and `make connector-validate` agree.
    """
    from chemclaw.agent.template_surface import resolvable_signatures

    missing_dep = ModuleNotFoundError("No module named 'rdkit'")
    missing_dep.name = "rdkit"

    def fail_for_bundles(name: str) -> Any:
        raise missing_dep

    monkeypatch.setattr("chemclaw.connectors.registry.importlib.import_module", fail_for_bundles)
    with pytest.raises(ModuleNotFoundError, match="rdkit"):
        resolvable_signatures()


# --- the run --------------------------------------------------------------------------


def test_a_template_run_executes_its_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """A template run executes its steps in order, end to end against a real Temporal server.

    The activities are recording stand-ins under the same names, so this tests the sequencer:
    substitution, ordering, each step's scope and the accumulated result.
    """
    from temporalio import activity
    from temporalio.worker import Worker

    from chemclaw.durable.template_activities import AgentStepInput, ToolStepInput
    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
    from tests.temporal_env import pydantic_client, start_env_or_skip

    seen: list[tuple[str, Any]] = []

    @activity.defn(name="run_tool_step")
    async def fake_tool(step: ToolStepInput) -> Any:
        seen.append(("tool", step.arguments))
        return {"flags": ["azide"]}

    @activity.defn(name="run_agent_step")
    async def fake_agent(step: AgentStepInput) -> str:
        seen.append(("agent", step.prompt))
        return "briefing text"

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Screen then write.",
            "inputs": [{"name": "smiles", "type": "string", "description": "molecule"}],
            "steps": [
                {
                    "id": "hazards",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": ["${inputs.smiles}"]},
                },
                {
                    "id": "brief",
                    "kind": "agent",
                    "prompt": "Flags for ${inputs.smiles}: ${steps.hazards.result}",
                },
            ],
        }
    )

    @activity.defn(name="completed_steps")
    async def fake_completed_steps(request: Any) -> dict[str, Any]:
        """Stand in for the resume read, which wants a record store this test does not configure.

        Answers `{}`, as a first run gets; without it the run stalls on an unserved activity.
        """
        return {}

    @activity.defn(name="record_job")
    async def fake_record_job(record: Any) -> None:
        """Stand in for the real record write, which wants a sink this test does not configure.

        Registered by the *name* the workflow dispatches, because Temporal routes on the activity
        name rather than the callable.
        """
        return None

    async def _run() -> Any:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-templates",
                    workflows=[TemplateWorkflow],
                    activities=[fake_tool, fake_agent],
                ),
                # The record write needs a worker, or the run only finishes when `record_job`'s
                # `schedule_to_start` expires and the test measures that timeout.
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    activities=[fake_record_job, fake_completed_steps],
                ),
            ):
                return await client.execute_workflow(
                    TemplateWorkflow.run,
                    TemplateRunInput(
                        template=template,
                        inputs={"smiles": "CCO"},
                        requested_by="tester",
                    ),
                    id="template-run-test",
                    task_queue="test-templates",
                )

    result = asyncio.run(_run())

    # Declared order, and each step saw its references already substituted.
    assert [kind for kind, _ in seen] == ["tool", "agent"]
    # A whole-string reference kept its type: the tool got a list, not the text of one.
    assert seen[0][1] == {"smiles": ["CCO"]}
    # An embedded reference interpolated the earlier step's result into the prompt.
    assert 'Flags for CCO: {"flags": ["azide"]}' == seen[1][1]
    # Every step's result is kept, not just the last — that is what an auditor asks for.
    assert result.steps == {"hazards": {"flags": ["azide"]}, "brief": "briefing text"}
    assert result.result == "briefing text"
    assert result.template == "probe"


# --- the agent step's retry is narrower than every other step's -------------------------------


async def test_only_the_agent_step_carries_the_narrowed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the `agent` step carries the narrowed retry.

    Replaying a whole agent turn on a provider blip would re-run every tool it already ran, so the
    agent step gets `agent_step_retry()`; a tool step keeps the normal transient-retry budget.
    Asserts what `_run_step` hands Temporal per branch, with the module's `workflow` handle
    substituted.
    """
    import types

    from chemclaw.durable import template_job
    from chemclaw.durable.publish import BAD_DATA_RETRY
    from chemclaw.durable.template_activities import StepIdentity

    seen: list[Any] = []

    async def execute_activity(*_args: Any, **kwargs: Any) -> str:
        seen.append(kwargs["retry_policy"])
        return "ok"

    monkeypatch.setattr(
        template_job, "workflow", types.SimpleNamespace(execute_activity=execute_activity)
    )

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Screen then write.",
            "steps": [
                {"id": "hazards", "kind": "tool", "tool": "screen_hazards", "arguments": {}},
                {"id": "brief", "kind": "agent", "prompt": "write it up"},
            ],
        }
    )
    identity = StepIdentity(actor="tester", roles=[], correlation_id="run-1")

    for step in template.steps:
        await template_job.TemplateWorkflow()._run_step(
            step, {}, identity, timedelta(seconds=60), template.name
        )

    tool_policy, agent_policy = seen
    assert tool_policy.maximum_attempts == BAD_DATA_RETRY.maximum_attempts
    assert agent_policy.maximum_attempts == settings.agent_step_max_attempts
    assert agent_policy.maximum_attempts < tool_policy.maximum_attempts
    # Narrower in attempts only: which failures count as transient must not depend on the branch.
    assert agent_policy.non_retryable_error_types == BAD_DATA_RETRY.non_retryable_error_types


# --- DARK-2: a connector tool step is governed exactly as an in-process one (D-168) ------------


class _Recorder:
    """An audit sink that keeps what it is handed."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        """Keep one event."""
        self.events.append(event)


def _fake_connector_tool(name: str, calls: list[dict[str, Any]]) -> Any:
    """A connector tool as `open_connector_specs` produces one: an ordinary LangChain tool.

    With one tool shape there is no second call path that could bypass audit and authorization.
    """

    @tool_decorator(name_or_callable=name, description="screen a molecule for hazards")
    async def _fake(smiles: list[str]) -> str:
        calls.append({"smiles": smiles})
        return "hazard: none found"

    return _fake


def _tool_step(tool: str, **arguments: Any) -> Any:
    from chemclaw.durable.template_activities import StepIdentity, ToolStepInput

    return ToolStepInput(
        tool=tool,
        arguments=dict(arguments),
        identity=StepIdentity(actor="chemist-1", roles=[], correlation_id="template-run-1"),
    )


def test_a_connector_tool_step_is_audited_under_the_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector tool step is audited under the requester."""
    from chemclaw.durable.template_activities import _invoke

    sink = _Recorder()
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: sink)
    calls: list[dict[str, Any]] = []
    tool = _fake_connector_tool("screen_hazards", calls)

    result = asyncio.run(_invoke([tool], _tool_step("screen_hazards", smiles=["CCO"]), []))

    assert result == "hazard: none found"
    assert calls == [{"smiles": ["CCO"]}]
    (event,) = sink.events
    assert (event.tool, event.actor, event.outcome) == ("screen_hazards", "chemist-1", "ok")
    assert event.correlation_id == "template-run-1"


def test_a_connector_tool_step_the_requester_may_not_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template must not be a way to run a tool you could not run directly.

    With the gate skipped it was exactly that: anyone who could start the template got every
    connector tool inside it, whatever `tool_role_gates` said.
    """
    from chemclaw.agent.authz import AuthorizationError
    from chemclaw.durable.template_activities import _invoke

    sink = _Recorder()
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: sink)
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_role_gates", {"screen_hazards": ["safety"]})
    calls: list[dict[str, Any]] = []
    tool = _fake_connector_tool("screen_hazards", calls)

    # It raises: a template step has no model, and a converted refusal would become the step's
    # `${steps.<id>.result}`. `invoke_governed` therefore folds only the governance half of the
    # chain.
    with pytest.raises(AuthorizationError):
        asyncio.run(_invoke([tool], _tool_step("screen_hazards", smiles=["CCO"]), []))

    assert calls == [], "the tool body ran despite the refusal"
    (event,) = sink.events
    assert event.outcome == "refused", "a denied connector step left no audit row"


def test_a_step_result_is_something_temporal_can_carry() -> None:
    """A step result is something Temporal can carry.

    MCP content blocks are not serializable, and a step result crosses an activity boundary; the
    in-process tests never serialize anything.
    """
    from chemclaw.durable.template_activities import _mcp_text

    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _mcp_text(blocks) == "a\nb"
    # Anything the converter already understands is handed through untouched.
    assert _mcp_text({"energy": -154.1}) == {"energy": -154.1}
    assert _mcp_text("plain") == "plain"


def test_a_structured_tool_result_is_not_mistaken_for_mcp_content() -> None:
    """A structured tool result is not mistaken for MCP content.

    `NoteRef` has a `type` field, so the check is "a list of dicts carrying a `type` key", which a
    list of pydantic models cannot satisfy.
    """
    from chemclaw.agent.graph_tools import NoteRef
    from chemclaw.durable.template_activities import _mcp_text

    notes = [NoteRef(id="reaction-1", type="reaction", source="eln", confidence=0.9)]
    assert _mcp_text(notes) is notes, "a structured result was flattened into a string"


def test_a_tool_steps_structured_content_reaches_the_next_step_as_a_model() -> None:
    """A tool step's structured content reaches the next step as a model.

    Templates pass one tool's field to the next (`${steps.forms.result.smiles}`), which needs the
    structured result rather than joined text. Asserted over the whole path, since each piece alone
    is correct and only the composition was wrong.
    """
    from langchain_core.tools import StructuredTool

    from chemclaw.durable.template_activities import (
        StepIdentity,
        ToolStepInput,
        _call_governed,
    )
    from chemclaw.templates.resolve import resolve

    payload = {"smiles": ["CC(=O)CC(C)=O", "CC(O)=CC(C)=O"], "count": 2}

    def _enumerate(smiles: str) -> tuple[list[dict[str, str]], dict[str, object]]:
        # The shape `langchain_mcp_adapters` produces: content blocks, plus the server's
        # `structuredContent` under the artifact's own key.
        return [{"type": "text", "text": json.dumps(payload)}], {"structured_content": payload}

    tool = StructuredTool.from_function(
        func=_enumerate,
        name="enumerate_tautomers",
        description="enumerate tautomers",
        response_format="content_and_artifact",
    )
    step = ToolStepInput(
        arguments={"smiles": "CC(=O)CC(C)=O"},
        identity=StepIdentity(actor="chemist@example.com", roles=[], correlation_id="c-1"),
        tool="enumerate_tautomers",
    )

    result = asyncio.run(_call_governed(tool, step))

    assert result == payload, "the structured content the server sent was discarded"
    # The reference the templates actually carry, against the value the step actually leaves.
    assert (
        resolve("${steps.forms.result.smiles}", {"steps.forms.result": result}) == payload["smiles"]
    )


def test_the_validator_refuses_an_empty_or_absent_templates_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator refuses an empty or absent templates directory.

    The templates back the `run_*` launchers, so a missing `data/templates/` or a mis-set
    `CHEMCLAW_TEMPLATES_DIR` must not print a green line.
    """
    from chemclaw.cli.validate_templates import validate_templates

    empty = tmp_path / "empty"
    empty.mkdir()
    for directory in (empty, tmp_path / "does-not-exist"):
        monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(directory))
        problems = validate_templates()
        assert any("no templates discovered" in problem for problem in problems), (
            f"{directory} produced {problems}"
        )


# --- the live argument check ----------------------------------------------------------
#
# For bundles this repository declares but does not run, `make live-template-args` checks
# arguments against a running server's advertised schema. Testable offline is its judgment: check
# what it can, refuse to invent what it cannot, and say which is which.


def _live_tool(name: str) -> Any:
    """A tool as an open connector session hands one over: an ordinary LangChain tool.

    Real rather than a stand-in, so `_live_arguments` reads a genuine `tool_call_schema` — the
    thing whose shape the live check depends on and which no assertion about a mock would pin.
    """

    @tool_decorator(name_or_callable=name, description="screen a molecule for hazards")
    async def _served(smiles: list[str], top_k: int = 5) -> str:
        return "hazard: none found"

    return _served


def _live_template(**arguments: Any) -> Template:
    """A one-step template calling `screen_hazards` with `arguments`."""
    return Template.model_validate(
        {
            "name": "probe",
            "summary": "Do the thing.",
            "steps": [
                {"id": "one", "kind": "tool", "tool": "screen_hazards", "arguments": arguments}
            ],
        }
    )


_OWNERS = {"screen_hazards": "safety"}


def test_the_live_check_reads_the_arguments_off_a_running_tool() -> None:
    """The whole point of the live lane: the authority is the schema the server advertised.

    Both directions, because both are run-time failures of a *pinned* procedure and neither is
    visible offline for this tool: a key the tool does not take, and a required key the step omits.
    """
    from chemclaw.cli.validate_template_args_live import check_live_arguments

    served = {"screen_hazards": _live_tool("screen_hazards")}
    report = check_live_arguments(
        [_live_template(smilez=["CCO"], nonexistent_arg=42)], _OWNERS, served, unreachable=()
    )
    assert any("does not take" in p and "nonexistent_arg" in p for p in report.problems), report
    assert any("omits required argument(s) ['smiles']" in p for p in report.problems), report

    # And it must not invent failures: `top_k` has a default, so omitting it is correct.
    good = check_live_arguments([_live_template(smiles=["CCO"])], _OWNERS, served, unreachable=())
    assert good.problems == []
    assert good.checked == ["probe/one -> screen_hazards (safety)"]
    assert good.unreached == {}


def test_the_live_check_reports_an_unreached_connector_instead_of_counting_it() -> None:
    """The live check reports an unreached connector instead of counting it.

    An unreached connector contributes no tools, so checks against it would pass vacuously; it is
    "unreached", neither a problem nor a pass, and `main` gives it a distinct exit code.
    """
    from chemclaw.cli.validate_template_args_live import check_live_arguments

    report = check_live_arguments(
        [_live_template(smilez=["CCO"])], _OWNERS, {}, unreachable=("safety",)
    )
    assert report.problems == []
    assert report.checked == []
    assert report.unreached == {"safety": ["probe/one -> screen_hazards"]}


def test_the_live_check_flags_a_tool_a_reachable_server_does_not_serve() -> None:
    """The live check flags a tool a reachable server does not serve.

    Once the connector is up, its silence about a tool is evidence, so it is a problem.
    """
    from chemclaw.cli.validate_template_args_live import check_live_arguments

    report = check_live_arguments([_live_template(smiles=["CCO"])], _OWNERS, {}, unreachable=())
    assert report.checked == []
    assert any("running server does not serve" in p for p in report.problems), report


def test_an_in_process_tool_is_left_to_the_offline_gate() -> None:
    """One question, one answer. A tool whose signature is in this tree is checked there, not twice.

    A second lane checking the same thing differently is how two gates end up disagreeing about
    one template — the failure `resolvable_signatures` already records for the import path.
    """
    from chemclaw.cli.validate_template_args_live import check_live_arguments

    report = check_live_arguments(
        [_live_template(anything=1)], owners={}, live_tools={}, unreachable=()
    )
    assert (report.problems, report.checked, report.unreached) == ([], [], {})


def test_both_lanes_derive_the_same_arguments_from_the_same_tool() -> None:
    """Both lanes derive the same arguments from the same tool.

    `ToolArguments` is built from an `inspect.Signature` offline and from an advertised schema live,
    with `argument_problems` the single reader; the constructors must agree.
    """
    import inspect

    from chemclaw.agent.template_surface import normalise_tool_schema
    from chemclaw.cli.validate_templates import ToolArguments

    async def screen_hazards(smiles: list[str], top_k: int = 5) -> str:
        """Screen a molecule for hazards."""
        return "hazard: none found"

    offline = ToolArguments.of_signature(inspect.signature(screen_hazards))
    live = ToolArguments.of_schema(normalise_tool_schema(_live_tool("screen_hazards")) or {})
    assert (
        offline
        == live
        == ToolArguments(
            accepted=frozenset({"smiles", "top_k"}),
            required=frozenset({"smiles"}),
            takes_any_key=False,
        )
    )


def test_the_validator_reports_an_invalid_manifest_as_a_problem_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid manifest is reported as a problem, not a traceback.

    `main` resolves the tool surface first, which loads the template registry, so the error must be
    caught there. It must still fail, but not look like a crash.
    """
    from chemclaw.cli.validate_templates import main

    (tmp_path / "forward.yaml").write_text(
        "summary: x\ninputs:\n  - {name: smiles, type: string, description: m}\n"
        "steps:\n  - {id: one, kind: agent, prompt: 'about ${inputs.nosuch}'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))

    assert main([]) == 1
    printed = capsys.readouterr().out
    assert "template validation failed:" in printed
    assert "unknown 'inputs.nosuch'" in printed


class _RefusingClient:
    """A Temporal client whose `start_workflow` fails, and which can describe the run it names.

    Both halves of the launch failure this pins are properties of the *client*, so the fake is one
    object: `error` is what the start raises, `status` is what a rejoined run describes as.
    """

    def __init__(self, error: Exception, status: WorkflowExecutionStatus | None = None) -> None:
        self.error = error
        self.status = status

    async def start_workflow(self, _run: Any, _arg: Any, **_kwargs: Any) -> Any:
        raise self.error

    def get_workflow_handle(self, workflow_id: str, **_kwargs: Any) -> Any:
        status = self.status

        class _Handle:
            id = workflow_id

            async def describe(self) -> Any:
                return type("Description", (), {"status": status})()

        return _Handle()


def _refusing(monkeypatch: pytest.MonkeyPatch, client: _RefusingClient) -> None:
    """Point the launcher at `client`, with an actor to attribute the launch to."""

    async def connect() -> _RefusingClient:
        return client

    monkeypatch.setattr("chemclaw.templates.registry.connect", connect)
    monkeypatch.setattr("chemclaw.templates.registry.require_actor", lambda: "chemist@lab")


def test_relaunching_a_running_template_announces_the_run_it_rejoined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relaunching a running template announces the run it rejoined.

    A duplicate launch is idempotency succeeding, so a `JobSignal` must still reach the turn and
    `started_jobs` must record it, or no later `job_completed` can clear it.
    """
    _refusing(
        monkeypatch,
        _RefusingClient(
            WorkflowAlreadyStartedError("already", "TemplateWorkflow", run_id=None),
            status=WorkflowExecutionStatus.RUNNING,
        ),
    )
    tool = build_template_tool(_template())

    run_id, signals = asyncio.run(collect_signals(lambda: tool(params={})))

    assert signals == [JobSignal(job_id=run_id, kind="template:probe")]


def test_a_rejoined_template_that_is_no_longer_running_is_not_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejoined template that is no longer `RUNNING` is not announced.

    A finished run will never emit the `job_completed` that clears an announced row; the id still
    comes back.
    """
    _refusing(
        monkeypatch,
        _RefusingClient(
            WorkflowAlreadyStartedError("already", "TemplateWorkflow", run_id=None),
            status=WorkflowExecutionStatus.COMPLETED,
        ),
    )
    tool = build_template_tool(_template())

    run_id, signals = asyncio.run(collect_signals(lambda: tool(params={})))

    assert signals == []
    assert run_id.startswith("template-probe-")


def test_a_broker_fault_at_launch_reaches_the_model_as_a_written_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker fault at launch reaches the model as a written refusal.

    An `RPCError` is neither a `ChemclawError` nor a transport failure, so it is framed like the
    connector-job launcher's `ConnectorJobError`, with the check to run before relaunching.
    """
    _refusing(monkeypatch, _RefusingClient(RPCError("no worker", RPCStatusCode.UNAVAILABLE, b"")))
    tool = build_template_tool(_template())

    with pytest.raises(TemplateError) as raised:
        asyncio.run(tool(params={}))

    assert isinstance(raised.value, ChemclawError)
    assert "get_durable_job_status" in str(raised.value), (
        "the refusal has to name the check a chemist runs before relaunching"
    )


# --- the runtime precondition ---------------------------------------------------------


def test_a_template_whose_steps_do_not_resolve_is_refused_before_anything_is_queued(
    client: _FakeClient,
) -> None:
    """A template whose steps do not resolve is refused before anything is queued.

    The launcher reuses the gate's `step_problems` rather than restating the rule, and refuses
    before dialling the client, so "nothing was queued" is true.
    """
    template = _template(
        steps=[{"id": "survey", "kind": "job", "job": "no_such_job", "arguments": {}}]
    )
    with pytest.raises(TemplateError) as raised:
        asyncio.run(build_template_tool(template)({}))
    message = str(raised.value)
    assert "no_such_job" in message
    assert "nothing was queued" in message.lower()
    assert client.calls == [], "the launcher dialled Temporal for a run it had already refused"


def test_a_template_whose_steps_resolve_still_launches(client: _FakeClient) -> None:
    """The precondition must refuse a broken template and *only* a broken one.

    A gate nobody has watched pass is as unproven as one nobody has watched refuse — and this one
    stands between every shipped procedure and its launch.
    """
    template = _template(
        steps=[{"id": "one", "kind": "tool", "tool": "find_past_jobs", "arguments": {}}]
    )
    job_id = asyncio.run(build_template_tool(template)({}))
    assert job_id == run_workflow_id(template, {})
    assert len(client.calls) == 1


def test_the_gate_and_the_launcher_share_one_definition_of_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rule, one implementation: the runtime precondition *is* the validator's own check.

    Stated as an identity rather than as two agreeing outputs, because two implementations that
    agree today are exactly what this repository keeps finding a year later.
    """
    from chemclaw.agent import template_surface
    from chemclaw.cli import validate_templates

    assert validate_templates.step_problems is template_surface.step_problems
    # The launcher's identity is proven by *substitution* rather than by a name it re-exports: it
    # imports the function lazily (the module edge would be a cycle), so only replacing the one
    # definition and watching the refusal change shows there is not a second one.
    calls: list[str] = []

    def _fake_rule(template: Any, surface: Any = None) -> list[str]:
        calls.append(template.name)
        return ["the shared rule spoke"]

    monkeypatch.setattr(template_surface, "step_problems", _fake_rule)
    assert registry.unrunnable_reason(_template()) == "  - the shared rule spoke"
    assert calls == ["probe"]


# --- the run ceiling has to cover the procedure, not one step of it ------------------------------


def _job_steps(count: int, *, chained: bool = True) -> list[dict[str, Any]]:
    """`count` `job` steps plus the `agent` step every shipped template ends with.

    `chained` makes each step read the one before it: chained steps are N waves and cost N
    ceilings, independent ones share a wave. The default is the sequential case the bound targets.
    """
    steps: list[dict[str, Any]] = [
        {
            "id": f"j{i}",
            "kind": "job",
            "job": "rank_species",
            "arguments": ({"species": f"${{steps.j{i - 1}.result}}"} if chained and i else {}),
        }
        for i in range(count)
    ]
    return [*steps, {"id": "report", "kind": "agent", "prompt": "sum it up"}]


def test_a_template_that_cannot_finish_inside_the_run_ceiling_is_refused() -> None:
    """A template that cannot finish inside the run ceiling is refused.

    `core/config` can only check one step against the ceiling, since it cannot see templates. An
    execution timeout is not delivered to workflow code, so a run outliving it stops with no failure
    row and no notice.
    """
    problems = run_ceiling_problems(_template(steps=_job_steps(2)))

    assert len(problems) == 1
    assert "cannot finish inside template_run_timeout_seconds" in problems[0]
    # The arithmetic, not just the verdict: an operator reading this has to know which step to
    # shorten, and "the template is too long" is unactionable over a procedure with five of them.
    assert "j0=39,330s" in problems[0]
    assert "j1=39,330s" in problems[0]


def test_the_refusals_printed_terms_add_up_to_the_printed_total() -> None:
    """The refusal's printed terms add up to its printed total.

    Concurrent wave members print as `" | "` inside brackets carrying the wave's cost, since `" + "`
    reads as addition; a wave of one prints as the bare step.
    """
    # Two independent `job` steps (one wave) and a third reading the first (a second wave): wide
    # enough to bracket, long enough to overflow. `_job_steps` gives one shape or the other.
    steps = [
        {"id": "j0", "kind": "job", "job": "rank_species", "arguments": {}},
        {"id": "j1", "kind": "job", "job": "rank_species", "arguments": {}},
        {
            "id": "j2",
            "kind": "job",
            "job": "rank_species",
            "arguments": {"species": "${steps.j0.result}"},
        },
        {"id": "report", "kind": "agent", "prompt": "sum it up"},
    ]
    problems = run_ceiling_problems(_template(steps=steps))
    one_job = settings.template_step_ceilings()["job"][0]

    assert len(problems) == 1
    assert "j0=39,330s | j1=39,330s | report=900s" in problems[0], (
        "concurrent members must not be joined with a separator that reads as addition"
    )
    assert f"[{one_job:,.0f}s: j0" in problems[0], "a wave has to state its own cost"
    # **The arithmetic a reader would do, done here.** The breakdown is the bracketed expression;
    # its `+`-separated terms are a wave's stated cost or a lone step's, and adding those has to
    # give the stated total. Under the old separator it did not, which is the whole finding.
    breakdown_match = re.search(r"take ([\d,]+)s in total \((.+?)\)\. Waves", problems[0])
    assert breakdown_match is not None, f"the refusal no longer states a breakdown: {problems[0]}"
    stated, breakdown = breakdown_match.groups()
    # The first figure of a term is what that term costs: a bracketed wave states its own cost
    # first, and a lone step's is its only figure.
    terms = [
        int(re.search(r"([\d,]+)s", term).group(1).replace(",", ""))  # type: ignore[union-attr]
        for term in breakdown.split(" + ")
    ]
    assert sum(terms) == int(stated.replace(",", "")), (
        f"the printed terms {terms} have to add up to the printed total {stated}"
    )


def test_a_wave_wider_than_the_deployment_runs_at_once_costs_more_than_one_step() -> None:
    """A wave wider than the deployment runs at once costs more than one step.

    `_run_wave` dispatches in batches of `TemplateRunInput.max_parallel_steps`, pinned at launch
    from the same setting, so the cost is per batch.
    """
    limit = settings.orchestrator_max_parallel_children
    fits = [
        {"id": f"t{i}", "kind": "tool", "tool": "enumerate_tautomers", "arguments": {}}
        for i in range(limit)
    ]
    assert run_ceiling_problems(_template(steps=fits)) == [], (
        "a wave no wider than the deployment runs at once still costs one step"
    )

    # Wide enough that `ceil(width / limit)` slow steps cannot fit, where one step trivially would.
    batches_needed = int(settings.template_run_timeout_seconds // 900) + 2
    wide = [
        {"id": f"t{i}", "kind": "tool", "tool": "enumerate_tautomers", "arguments": {}}
        for i in range(limit * batches_needed)
    ]
    problems = run_ceiling_problems(_template(steps=wide))

    assert len(problems) == 1, "a wave nobody could run in one batch has to be refused"
    assert f"at most {limit} at a time" in problems[0]


def test_a_wave_is_charged_what_its_batches_cost_and_not_its_slowest_step_per_batch() -> None:
    """A wave is charged what its batches cost, not its slowest step per batch.

    One `job` step beside eight `tool` steps is one wave in two batches, costing 39,330 + 900, not
    2 × 39,330. Over-stating refuses procedures that would have finished.
    """
    limit = settings.orchestrator_max_parallel_children
    one_job = settings.template_step_ceilings()["job"][0]
    one_tool = settings.template_step_ceilings()["tool"][0]
    steps: list[dict[str, Any]] = [{"id": "j0", "kind": "job", "job": "rank_species"}]
    steps += [
        {"id": f"t{index}", "kind": "tool", "tool": "enumerate_tautomers", "arguments": {}}
        for index in range(limit)
    ]
    steps.append({"id": "say", "kind": "agent", "prompt": "sum it up: ${steps.j0.result}"})
    template = _template(steps=steps)

    # One wave of `limit + 1`, so exactly two batches: [job + limit-1 tools] then [one tool].
    (wave, second) = schedule(template)
    assert len(wave) == limit + 1 and len(second) == 1
    charged = one_job + one_tool
    assert charged + one_tool <= settings.template_run_timeout_seconds, (
        "the fixture has to be a procedure that genuinely fits, or this asserts nothing"
    )

    assert run_ceiling_problems(template) == [], (
        f"a wave costing {charged:,.0f}s must not be charged {2 * one_job:,.0f}s"
    )


def test_a_wave_is_split_into_batches_of_the_bound_it_was_sized_with() -> None:
    """The split itself: declared order kept, nothing dropped, `0` meaning one batch.

    A fixed-size batch rather than a semaphore, for `fan_out`'s reason: it does not depend on
    lock-acquisition order, so it is deterministic under replay.
    """
    from chemclaw.templates.schedule import batches

    wave = tuple(range(9))

    assert batches(wave, 4) == ((0, 1, 2, 3), (4, 5, 6, 7), (8,))
    assert [step for batch in batches(wave, 4) for step in batch] == list(wave), (
        "flattening the batches must reproduce the wave, or a step is dropped or reordered"
    )
    # `0` is what an input predating the field declares, and every archived history is pre-wave —
    # so it has to mean "one batch", which over a one-step wave is the sequential shape byte for
    # byte.
    assert batches(wave, 0) == (wave,)
    assert batches(wave, 99) == (wave,)
    assert batches((0,), 0) == ((0,),)


def test_the_run_dispatches_a_wave_in_batches_rather_than_all_at_once() -> None:
    """The run dispatches a wave in batches rather than all at once.

    The enforcing half of the ceiling arithmetic. Driven against `_run_wave` with an injected
    `_run_step`, since the subject is the dispatch shape, not the broker.
    """
    from chemclaw.durable.template_activities import StepIdentity
    from chemclaw.durable.template_job import TemplateWorkflow

    in_flight = 0
    high_water = 0

    # `self` explicitly: patched onto the class, so the descriptor binds it as the first argument
    # and a signature starting at `step` silently receives the workflow instead.
    async def _step(_self: Any, step: Any, *_args: Any, **_kwargs: Any) -> str:
        nonlocal in_flight, high_water
        in_flight += 1
        high_water = max(high_water, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return f"ran-{step}"

    workflow = TemplateWorkflow()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(TemplateWorkflow, "_run_step", _step)
        wave = tuple(f"s{index}" for index in range(9))
        done = asyncio.run(
            workflow._run_wave(
                wave,
                {},
                StepIdentity(actor="tester", roles=[], correlation_id="wave-probe"),
                timedelta(seconds=1),
                "probe",
                4,
            )
        )
    finally:
        monkey.undo()

    assert high_water == 4, f"at most 4 may be in flight at once, saw {high_water}"
    assert [step for step, _ in done] == list(wave), "results come back in the wave's own order"
    assert [result for _, result in done] == [f"ran-{step}" for step in wave]


def test_a_launch_pins_the_bound_the_ceiling_checked_it_against(client: _FakeClient) -> None:
    """A launch pins the parallelism bound the ceiling was checked against.

    A settings read inside workflow code would be nondeterministic on replay and could differ from
    what `run_ceiling_problems` used.
    """
    from chemclaw.templates.registry import build_template_tool

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "orchestrator_max_parallel_children", 3)
        tool = build_template_tool(
            _template(inputs=[{"name": "smiles", "type": "string", "description": "the molecule"}])
        )
        asyncio.run(tool(params={"smiles": "CCO"}))
    finally:
        monkey.undo()

    (call,) = client.calls
    assert call["input"].max_parallel_steps == 3, (
        "the launch has to pin the deployment's bound into the run, or the ceiling sized it with a "
        "number the run never sees"
    )


def test_one_job_step_still_fits_so_the_gate_is_not_simply_refusing_job_steps() -> None:
    """The control arm. Seven of the nine shipped templates have a `job` step and must still run."""
    assert run_ceiling_problems(_template(steps=_job_steps(1))) == []


def test_two_job_steps_that_do_not_read_each_other_fit_because_they_share_a_wave() -> None:
    """Two `job` steps that do not read each other fit, because they share a wave.

    The bound follows the schedule; a flat sum would refuse a procedure that would have finished.
    """
    assert run_ceiling_problems(_template(steps=_job_steps(2, chained=False))) == []
    assert run_ceiling_problems(_template(steps=_job_steps(2, chained=True))) != []


@pytest.mark.parametrize("name", sorted(registry.discovered()))
def test_every_shipped_template_fits_this_deployments_run_ceiling(name: str) -> None:
    """Every shipped template fits this deployment's run ceiling.

    Per template, so a failure names the file.
    """
    assert run_ceiling_problems(registry.discovered()[name]) == []


def test_the_launcher_refuses_a_run_the_ceiling_cannot_hold_before_anything_is_queued() -> None:
    """The launcher refuses a run the ceiling cannot hold before anything is queued.

    The gate and the launcher share `unrunnable_reason`, so they cannot drift apart.
    """
    blocked = registry.unrunnable_reason(_template(steps=_job_steps(2)))

    assert "cannot finish inside template_run_timeout_seconds" in blocked


def test_the_config_floor_and_the_template_gate_read_one_step_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config floor and the template gate read one step-ceiling definition.

    Checked by moving the setting the `job` ceiling is built from and watching the gate's answer
    move, rather than by asserting a transcribed total.
    """
    ceilings = settings.template_step_ceilings()
    assert set(ceilings) == {"tool", "agent", "job"}, "a step kind sized nowhere counts as free"
    before = ceilings["job"][0]
    one_job = _template(steps=_job_steps(1))
    assert run_ceiling_problems(one_job) == []

    monkeypatch.setattr(
        settings,
        "connector_job_timeout_seconds",
        settings.connector_job_timeout_seconds + settings.template_run_timeout_seconds,
    )

    # The gate now refuses the same file, which is only possible if it is reading the same
    # definition the config validator does rather than a second copy of the arithmetic.
    assert settings.template_step_ceilings()["job"][0] > before
    assert run_ceiling_problems(one_job) != []


# --- independent steps run at the same time, and dependent ones still do not ---------------------


def _concurrency_probe(run_id: str, steps: list[dict[str, Any]]) -> tuple[float, list[str], Any]:
    """Run `steps` end to end against a real Temporal server and measure the overlap.

    Each `tool` step sleeps `_STEP_SECONDS` and records entry and exit times; whether activities ran
    together is invisible to a call count.
    """
    from temporalio import activity
    from temporalio.worker import Worker

    from chemclaw.durable.template_activities import AgentStepInput, ToolStepInput
    from chemclaw.durable.template_job import TemplateRunInput, TemplateWorkflow
    from tests.temporal_env import pydantic_client, start_env_or_skip

    order: list[str] = []
    live = 0
    peak = 0

    @activity.defn(name="run_tool_step")
    async def slow_tool(step: ToolStepInput) -> Any:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        order.append(str(step.arguments.get("smiles")))
        await asyncio.sleep(_STEP_SECONDS)
        live -= 1
        return {"ran": step.arguments.get("smiles")}

    @activity.defn(name="run_agent_step")
    async def fake_agent(step: AgentStepInput) -> str:
        return "done"

    @activity.defn(name="completed_steps")
    async def fake_completed_steps(request: Any) -> dict[str, Any]:
        """Stand in for the resume read, which wants a record store this test does not configure.

        Answers `{}`, as a first run gets; without it the run stalls on an unserved activity.
        """
        return {}

    @activity.defn(name="record_job")
    async def fake_record_job(record: Any) -> None:
        return None

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Concurrency probe.",
            "inputs": [{"name": "smiles", "type": "string", "description": "molecule"}],
            "steps": steps,
        }
    )

    async def _run() -> Any:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-parallel",
                    workflows=[TemplateWorkflow],
                    activities=[slow_tool, fake_agent],
                    # Or the two activities queue behind one another on the worker and this
                    # measures the worker's slot count instead of the sequencer's schedule.
                    max_concurrent_activities=4,
                ),
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    activities=[fake_record_job, fake_completed_steps],
                ),
            ):
                return await client.execute_workflow(
                    TemplateWorkflow.run,
                    TemplateRunInput(
                        template=template, inputs={"smiles": "CCO"}, requested_by="tester"
                    ),
                    # Unique per probe: a Temporal id is an idempotency key, so a shared one would
                    # rejoin the first probe's finished run and measure nothing.
                    id=f"template-parallel-{run_id}",
                    task_queue="test-parallel",
                )

    started = time.perf_counter()
    result = asyncio.run(_run())
    return time.perf_counter() - started, order, (peak, result)


#: Long enough that two overlapping steps are distinguishable from two sequential ones against
#: Temporal's own dispatch latency, short enough to keep the suite honest about its runtime.
_STEP_SECONDS = 1.0


def test_two_steps_that_do_not_read_each_other_run_at_the_same_time() -> None:
    """Two steps that do not read each other run at the same time.

    Concurrency is derived from the declared `${steps.<id>.result}` edges, so a procedure gets it by
    not stating a dependency it does not have.
    """
    elapsed, _order, (peak, result) = _concurrency_probe(
        "independent",
        [
            {"id": "a", "kind": "tool", "tool": "screen_hazards", "arguments": {"smiles": "a"}},
            {"id": "b", "kind": "tool", "tool": "screen_hazards", "arguments": {"smiles": "b"}},
            {"id": "sum", "kind": "agent", "prompt": "${steps.a.result} ${steps.b.result}"},
        ],
    )

    assert peak == 2, f"the two independent steps never overlapped (peak in flight: {peak})"
    assert elapsed < _STEP_SECONDS * 2, f"two 1s steps took {elapsed:.2f}s — they serialised"
    # And the results are still keyed and complete, which is what a wave must not cost.
    assert result.steps["a"] == {"ran": "a"}
    assert result.steps["b"] == {"ran": "b"}


def test_a_step_that_reads_another_still_waits_for_it() -> None:
    """A step that reads another still waits for it.

    Running a chain together would hand a step a `${steps.<id>.result}` that does not exist yet.
    """
    elapsed, order, (peak, _result) = _concurrency_probe(
        "chained",
        [
            {"id": "a", "kind": "tool", "tool": "screen_hazards", "arguments": {"smiles": "a"}},
            {
                "id": "b",
                "kind": "tool",
                "tool": "screen_hazards",
                "arguments": {"smiles": "${steps.a.result.ran}"},
            },
            # Embedded, not a whole-string reference: a whole-string one substitutes the
            # *value* with its type preserved, and a `tool` step's dict is not a `str` — so
            # `AgentStepInput` refuses it at the activity boundary.
            {"id": "sum", "kind": "agent", "prompt": "saw ${steps.b.result}"},
        ],
    )

    assert peak == 1, "a dependent step ran beside the step it reads"
    assert elapsed >= _STEP_SECONDS * 2
    assert order == ["a", "a"], order


def test_the_shipped_catalogue_is_scheduled_the_way_its_files_are_written() -> None:
    """What each shipped template's schedule actually is, so a YAML edit that changes it is seen.

    A number is not written here: the assertion is that a template's waves are exactly its
    dependency structure, which is re-derived from the same files the sequencer reads.
    """
    for name, template in sorted(registry.discovered().items()):
        waves = schedule(template)
        assert [step for wave in waves for step in wave] == list(template.steps), name
        for index, wave in enumerate(waves):
            earlier = {step.id for before in waves[:index] for step in before}
            for step in wave:
                assert dependencies(step) <= earlier, f"{name}: {step.id} runs before what it reads"


# --- a failed run resumes rather than starting over -----------------------------------------------


def _resumable_run(
    fail_on: set[str], resume_from: dict[str, Any], fingerprint: str | None = None
) -> tuple[list[str], Any]:
    """Run a three-step chain end to end, failing the named steps, and report which steps ran.

    `resume_from` is the resume read's answer in the shape a real `job_records` row holds, so this
    drives the sequencer's own skip decision.
    """
    from temporalio import activity
    from temporalio.worker import Worker

    from chemclaw.durable.template_activities import (
        AgentStepInput,
        ResumeRequest,
        ToolStepInput,
    )
    from chemclaw.durable.template_job import (
        TemplateRunInput,
        TemplateWorkflow,
        template_fingerprint,
    )
    from tests.temporal_env import pydantic_client, start_env_or_skip

    ran: list[str] = []

    template = Template.model_validate(
        {
            "name": "probe",
            "summary": "Resume probe.",
            "inputs": [{"name": "smiles", "type": "string", "description": "molecule"}],
            "steps": [
                {
                    "id": "one",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": "${inputs.smiles}"},
                },
                {
                    "id": "two",
                    "kind": "tool",
                    "tool": "screen_hazards",
                    "arguments": {"smiles": "${steps.one.result.ok}"},
                },
                {"id": "three", "kind": "agent", "prompt": "saw ${steps.two.result}"},
            ],
        }
    )

    @activity.defn(name="run_tool_step")
    async def tool_step(step: ToolStepInput) -> Any:
        name = str(step.arguments.get("smiles"))
        ran.append(name)
        if name in fail_on:
            raise ValueError(f"{name} was told to fail")
        return {"ok": "two" if name == "CCO" else "done"}

    @activity.defn(name="run_agent_step")
    async def agent_step(step: AgentStepInput) -> str:
        ran.append("three")
        return "final"

    @activity.defn(name="record_job")
    async def record(record: Any) -> None:
        return None

    # Annotated with the real type, not `Any`: the pydantic data converter decodes an activity's
    # argument from its hint, so `Any` hands the body a bare dict and the fingerprint comparison
    # below silently becomes an `AttributeError` inside the worker.
    @activity.defn(name="completed_steps")
    async def resume(request: ResumeRequest) -> dict[str, Any]:
        stored = {
            "steps": resume_from,
            "template_fingerprint": (
                fingerprint if fingerprint is not None else template_fingerprint(template)
            ),
        }
        # The activity's own three conditions live in `template_activities.completed_steps`; what
        # this stands in for is the row it reads, so the fingerprint comparison is exercised here
        # exactly as the real one does it.
        if stored["template_fingerprint"] != request.fingerprint:
            return {}
        return dict(resume_from)

    async def _run() -> Any:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-resume",
                    workflows=[TemplateWorkflow],
                    activities=[tool_step, agent_step],
                ),
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    activities=[record, resume],
                ),
            ):
                return await client.execute_workflow(
                    TemplateWorkflow.run,
                    TemplateRunInput(
                        template=template, inputs={"smiles": "CCO"}, requested_by="tester"
                    ),
                    id=f"template-resume-{sorted(fail_on)}-{sorted(resume_from)}-{fingerprint}",
                    task_queue="test-resume",
                )

    return ran, _run


def test_a_resumed_run_does_not_redo_the_steps_that_already_finished() -> None:
    """A resumed run does not redo the steps that already finished."""
    ran, run = _resumable_run(fail_on=set(), resume_from={"one": {"ok": "two"}})

    result = asyncio.run(run())

    # Step one never ran: its result came from the record. Step two ran, and read what step one
    # produced on the attempt that did run it.
    assert ran == ["two", "three"], ran
    assert result.steps["one"] == {"ok": "two"}
    assert result.result == "final"


def test_a_resume_is_refused_when_the_template_has_changed_under_the_same_id() -> None:
    """A resume is refused when the template has changed under the same id.

    A run id hashes the name and inputs only, so an edited file relaunches under the same id; mixing
    two definitions' results would give a wrong answer.
    """
    ran, run = _resumable_run(
        fail_on=set(), resume_from={"one": {"ok": "two"}}, fingerprint="a-different-template"
    )

    asyncio.run(run())

    # Everything ran: the stored steps were declined, so the run started over.
    assert ran == ["CCO", "two", "three"], ran


def test_a_first_run_with_nothing_to_resume_is_what_it_always_was() -> None:
    """The control arm: resume is unconditional, so the empty answer has to cost nothing."""
    ran, run = _resumable_run(fail_on=set(), resume_from={})

    result = asyncio.run(run())

    assert ran == ["CCO", "two", "three"], ran
    assert result.result == "final"


# --- a run id is an identity, and a composed workflow's name is not one --------------------------


def test_two_documents_sharing_a_name_do_not_share_a_run() -> None:
    """Two documents sharing a name do not share a run.

    The launcher rejoins an existing id. For a reviewed file template, sharing is right; a composed
    workflow's name is one chemist's and its steps change, so `scope` keeps two such documents from
    colliding on one run id and returning each other's results.
    """
    first = _template(name="triage", steps=[{"id": "x", "kind": "agent", "prompt": "one"}])
    second = _template(name="triage", steps=[{"id": "x", "kind": "agent", "prompt": "different"}])
    inputs = {"smiles": "CCO"}

    assert registry.run_workflow_id(first, inputs) == registry.run_workflow_id(second, inputs)
    scoped = {
        registry.run_workflow_id(first, inputs, "alice:fp-1"),
        registry.run_workflow_id(second, inputs, "alice:fp-2"),
        registry.run_workflow_id(first, inputs, "bob:fp-1"),
    }
    assert len(scoped) == 3, "a scope must separate owners and versions"


def test_a_file_templates_run_id_is_exactly_what_it_was() -> None:
    """A file template's run id is exactly what it was.

    `scope` defaults to empty, so existing ids in histories, retention and fixtures are unchanged.
    """
    template = _template(name="triage", steps=[{"id": "x", "kind": "agent", "prompt": "one"}])

    assert registry.run_workflow_id(template, {"smiles": "CCO"}) == (
        "template-triage-65f5e26304a2c36c"
    )


def test_a_composed_run_says_so_on_the_sessions_started_jobs_list() -> None:
    """Provenance a reader can see: a composed run is not a reviewed file with the same name."""
    assert registry.run_workflow_id(
        _template(name="triage", steps=[{"id": "x", "kind": "agent", "prompt": "one"}]),
        {},
        "alice:fp",
    ).startswith("composed-")


@pytest.mark.parametrize("actor", ["", " ", "\t\n"])
def test_a_step_identity_refuses_a_blank_actor(actor: str) -> None:
    """A whitespace actor is a principal nobody is, and every step would stamp it ambient."""
    from chemclaw.durable.template_activities import StepIdentity

    with pytest.raises(ValidationError):
        StepIdentity(actor=actor, correlation_id="run-1")


def test_a_step_identity_strips_the_actor_it_keeps() -> None:
    """Padding is not part of the principal: the stamped actor is the stripped one."""
    from chemclaw.durable.template_activities import StepIdentity

    assert StepIdentity(actor="  alice  ", correlation_id="run-1").actor == "alice"
