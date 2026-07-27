"""Step templates: the contract, the substitution, and the run — Stage E.

A template's whole promise is that the order does not vary and the run is reproducible, so the tests
that matter are the ones that would let that promise quietly break:

- a reference that does not resolve must stop the template from *starting*, not produce `None`
  halfway through a durable run that has already spent compute;
- substitution must preserve types, or a tool wanting a list silently receives its `repr`;
- the definition must be pinned into the run, or editing a file changes what is already executing —
  which is both a correctness bug and a Temporal replay violation.

The end-to-end run needs a Temporal server and is skipped offline like every other workflow test
here; everything above it is sandbox-safe and always runs.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from templates.manifest import AgentStep, Template
from templates.registry import (
    TemplateError,
    build_template_tool,
    discovered,
    run_workflow_id,
    tool_name,
)
from templates.resolve import UnresolvedReference, resolve

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
    monkeypatch.setattr("chemclaw.config.settings.templates_dir", str(tmp_path))
    discovered.cache_clear()
    try:
        with pytest.raises(TemplateError, match="invalid template"):
            discovered()
    finally:
        discovered.cache_clear()


def test_the_name_lives_in_the_filename_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for a profile: two sources of truth for one identity is drift waiting to happen."""
    (tmp_path / "named.yaml").write_text(
        "name: something-else\nsummary: x\nsteps:\n  - {id: one, kind: agent, prompt: hi}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("chemclaw.config.settings.templates_dir", str(tmp_path))
    discovered.cache_clear()
    try:
        with pytest.raises(TemplateError, match="name is its filename"):
            discovered()
    finally:
        discovered.cache_clear()


def test_the_validator_catches_a_step_naming_a_tool_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's reason to exist: a pinned procedure must not fail on step four in production."""
    from scripts.validate_templates import validate_templates

    (tmp_path / "ghost.yaml").write_text(
        "summary: x\nsteps:\n  - {id: one, kind: tool, tool: no_such_tool}\n", encoding="utf-8"
    )
    monkeypatch.setattr("chemclaw.config.settings.templates_dir", str(tmp_path))
    discovered.cache_clear()
    try:
        problems = validate_templates()
    finally:
        discovered.cache_clear()
    assert any("unknown tool 'no_such_tool'" in problem for problem in problems)


# --- the run --------------------------------------------------------------------------


def test_a_template_run_executes_its_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point, end to end against a real Temporal server: fixed order, results accumulated.

    The two activities are replaced by recording stand-ins registered under the same names, so this
    tests the *sequencer* — substitution, ordering, the scope each step sees, the accumulated result
    — rather than re-testing tool invocation, which `test_connector_safety_rubric.py` covers.
    """
    from temporalio import activity
    from temporalio.worker import Worker

    from tests.temporal_env import pydantic_client, start_env_or_skip
    from workflows.template_activities import AgentStepInput, ToolStepInput
    from workflows.template_job import TemplateRunInput, TemplateWorkflow

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

    async def _run() -> Any:
        async with await start_env_or_skip() as env:
            client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-templates",
                workflows=[TemplateWorkflow],
                activities=[fake_tool, fake_agent],
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
