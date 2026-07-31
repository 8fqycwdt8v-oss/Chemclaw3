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

from chemclaw.core.config import settings
from chemclaw.templates.manifest import AgentStep, Template
from chemclaw.templates.registry import (
    TemplateError,
    build_template_tool,
    discovered,
    run_workflow_id,
    tool_name,
)
from chemclaw.templates.resolve import UnresolvedReference, resolve

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
    """MAF publishes the params model's schema but passes the body a decoded `dict`.

    Every test above this one checked the generated tool's *name*, *docstring* and *schema*; none
    ever called it. So `launch` carried `cast(BaseModel, params).model_dump(...)` — a `cast` is a
    static no-op — and raised `AttributeError: 'dict' object has no attribute 'model_dump'` on
    every call. The shipped `hazard-briefing` template had never once run from a conversation, and
    `make template-validate` could not see it because it validates declarations, not invocation.
    Same defect as D-138, which fixed only the connector-job sibling.
    """
    tool = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    run_id = asyncio.run(tool(params={"smiles": "CCO"}))
    (call,) = client.calls
    assert call["input"].inputs == {"smiles": "CCO"}
    assert run_id == call["id"]


def test_launching_validates_rather_than_passing_the_object_through(client: _FakeClient) -> None:
    """The dict is *validated*, not merely accepted — the declared types are the contract.

    Without this the fix could be a `dict(params)`, which would forward whatever arrived and let a
    wrong-typed input reach a durable run that has already spent compute.
    """
    tool = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    with pytest.raises(ValidationError):
        asyncio.run(tool(params={"wrong_field": "CCO"}))


def test_launching_survives_the_frameworks_own_invocation_path(client: _FakeClient) -> None:
    """Driven through `agent_framework.tool(...).invoke()` rather than through our idea of it.

    The test above pins today's observed behaviour; this one pins the property that survives the
    framework changing its mind — whatever MAF hands the body, a launch through MAF's own
    dispatcher starts the run.
    """
    from agent_framework import tool as as_tool

    fn = build_template_tool(
        _template(inputs=[{"name": "smiles", "type": "string", "description": "The molecule."}])
    )
    invocable = as_tool(fn, name=fn.__name__, description="Run the probe template.")
    asyncio.run(invocable.invoke(arguments={"params": {"smiles": "CCO"}}))
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
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
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
    from chemclaw.cli.validate_templates import validate_templates

    (tmp_path / "ghost.yaml").write_text(
        "summary: x\nsteps:\n  - {id: one, kind: tool, tool: no_such_tool}\n", encoding="utf-8"
    )
    monkeypatch.setattr("chemclaw.core.config.settings.templates_dir", str(tmp_path))
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


# --- DARK-2: a connector tool step is governed exactly as an in-process one (D-158) ------------


class _Recorder:
    """An audit sink that keeps what it is handed."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        """Keep one event."""
        self.events.append(event)


class _FakeMcpFunction:
    """A stand-in for the `FunctionTool` MAF builds per MCP tool.

    Only two things matter about the real one and both are reproduced: it has a `name` the
    middleware reads, and `invoke(arguments=..., skip_parsing=True)` returns the connector's raw
    result. That is the whole interface the step uses.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, *, arguments: Any = None, skip_parsing: bool = False, **_: Any) -> Any:
        """Record the call and hand back a raw result, as `call_tool` used to."""
        self.calls.append(dict(arguments or {}))
        assert skip_parsing, (
            "the connector branch must not re-wrap the result (see _call_function_tool)"
        )
        return "hazard: none found"


class _FakeConnector:
    """A connector exposing one function, with the `call_tool` the step must no longer use."""

    def __init__(self, function: _FakeMcpFunction) -> None:
        self.functions = [function]
        self.call_tool_used = False

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        """The ungoverned path. Reaching it is the defect, so reaching it fails the test."""
        self.call_tool_used = True
        raise AssertionError(
            f"the template step called {name!r} through connector.call_tool, which skips both "
            "enforce_tool_authz and the audit middleware"
        )


class _EmptyAgent:
    """An agent whose in-process tool list is empty, so lookup falls through to the connector."""

    default_options: dict[str, Any] = {"tools": []}


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
    """Both tool steps of the shipped `hazard-briefing` used to leave no GxP audit row at all.

    The in-process branch hand-applied audit + authz; the connector branch two lines below called
    `connector.call_tool` and reached the connector directly. The module's own docstring said
    applying them was the point of the module.
    """
    from chemclaw.durable.template_activities import _invoke

    sink = _Recorder()
    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: sink)
    function = _FakeMcpFunction("screen_hazards")
    connector = _FakeConnector(function)

    result = asyncio.run(
        _invoke(_EmptyAgent(), [connector], _tool_step("screen_hazards", smiles=["CCO"]), [])
    )

    assert result == "hazard: none found", "the step's result shape changed; templates would break"
    assert function.calls == [{"smiles": ["CCO"]}]
    assert not connector.call_tool_used
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
    function = _FakeMcpFunction("screen_hazards")
    connector = _FakeConnector(function)

    with pytest.raises(AuthorizationError):
        asyncio.run(
            _invoke(_EmptyAgent(), [connector], _tool_step("screen_hazards", smiles=["CCO"]), [])
        )

    assert function.calls == [], "the tool body ran despite the refusal"
    (event,) = sink.events
    assert event.outcome == "error", "a denied connector step left no audit row"
