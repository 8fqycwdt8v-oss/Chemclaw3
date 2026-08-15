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
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool as tool_decorator
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
    """The params model's schema is published, but the body is passed a decoded `dict`.

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
    """Driven through the framework's own dispatcher rather than through our idea of it.

    The test above pins today's observed behaviour; this one pins the property that survives the
    framework changing its mind — whatever the dispatcher hands the body, a launch through it
    starts the run. MAF's `tool(...).invoke()` before the rebuild, LangChain's
    `StructuredTool.ainvoke` now.
    """
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
    """Checking the tool's *name* is half a reference; the arguments are the other half.

    Measured on the unfixed validator, this file — with `smiles` misspelt and a stray key beside
    it — printed "template validation passed." A pinned procedure that validates and then fails at
    the first live step, inside an activity after the launch, is the failure the gate exists to
    prevent, and the gate had it.

    It used to be written on `screen_hazards`, which is exactly the tool that can no longer be
    argument-checked at all — its bundle is declared here and served by `Chemclaw3-mcp`, so there
    is no local signature to read. Rewriting it onto `predict_pka`, a tool whose implementation is
    still in this tree, keeps this test about the check rather than about the gap; the gap has its
    own test below, because swapping the tool and saying nothing is how a gate quietly shrinks.
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
    """The argument check's blind spot is reported by name, not left to be inferred from silence.

    A bundle this release declares but does not run has no `server/tools.py` here, so its tools'
    signatures are unresolvable and `_step_problems` skips them — silently, by design, because an
    unresolvable tool must not produce invented failures. `hazard-briefing` calls `screen_hazards`,
    which makes the shipped template the first one that is name-checked and *not* argument-checked.

    The assertion is deliberately on the real shipped template rather than a fixture: what would
    go wrong is not the reporting mechanism, it is somebody moving another bundle out and not
    noticing that a pinned procedure lost its argument check. This fails the moment that happens
    and the note stops matching what ships.
    """
    from chemclaw.cli.validate_templates import unchecked_arguments

    assert unchecked_arguments() == {"hazard-briefing": ["screen_hazards"]}


def test_the_validator_accepts_a_correct_tool_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The argument check must not invent failures — the counterpart to the test above.

    A gate that rejects correct input is worse than one that misses bad input, because the first
    thing anyone does about it is switch it off. `top_k` is optional and deliberately omitted here,
    so this also pins that "required" is read off the signature's defaults rather than from the
    whole parameter list.
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
    """The argument check's coverage must not depend on which function ran first.

    `_resolvable_signatures()` reads `registered_tools()`, which is populated only as an import
    side effect of the agent package — and that import was supplied by `_step_problems` happening
    to call `_available_tools()` two lines earlier. Measured in a fresh interpreter before the fix:

        _resolvable_signatures() alone    -> 30 signatures, 31 advertised tools uncovered
        _available_tools() first, then it -> 50 signatures, 11 uncovered

    So reordering those two lines, or calling the function from anywhere else, silently dropped 20
    in-process tools from the check and the validator still printed "template validation passed" —
    a gate that quietly checks less is the exact failure mode `make template-validate` exists to
    close for templates. Run in a subprocess because the registry cannot be un-populated once this
    test session has imported the agent for something else.
    """
    probe = (
        "from chemclaw.cli.validate_templates import _available_tools, _resolvable_signatures\n"
        "first = set(_resolvable_signatures())\n"
        "_available_tools()\n"
        "print('SAME' if first == set(_resolvable_signatures()) else 'DIFFERENT', len(first))\n"
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
    """The other way into the coverage loss the test above measures — and it was still open.

    `_resolvable_signatures` caught every `ImportError`, so a bundle whose dependency stack is
    missing or renamed was indistinguishable from `qm`, which legitimately has no server module.
    Measured on this tree with one missing import injected into a connector's server tools module:
    50 signatures became 46 and `make template-validate` printed "template validation passed" and
    exited 0, while `make connector-validate` named the same bundle as broken. Two gates, one
    situation, opposite answers — so the import is now one shared function that raises, and this
    pins the raising half.
    """
    from chemclaw.cli.validate_templates import _resolvable_signatures

    missing_dep = ModuleNotFoundError("No module named 'rdkit'")
    missing_dep.name = "rdkit"

    def fail_for_bundles(name: str) -> Any:
        raise missing_dep

    monkeypatch.setattr("chemclaw.connectors.registry.importlib.import_module", fail_for_bundles)
    with pytest.raises(ModuleNotFoundError, match="rdkit"):
        _resolvable_signatures()


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


# --- the agent step's retry is narrower than every other step's -------------------------------


def test_only_the_agent_step_carries_the_narrowed_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch, not the policy object — which branch actually got which bound.

    `tests/test_publish.py` proves `agent_step_retry()` is narrower than `BAD_DATA_RETRY`. That is
    worth nothing on its own: a policy nobody passes is a policy nobody has, and the defect this
    guards is a whole turn being replayed on a provider blip, re-running every tool the failed
    attempt already ran (measured: one 503 → two PR-gate branches and two audit rows for one
    logical note). So this asserts what `_run_step` hands to Temporal, per branch.

    Both directions matter and the tool branch is the one at risk. A future edit that narrowed
    *every* step to one attempt would fix nothing and cost the transient-retry budget every other
    activity is deliberately given — a tool step recomputes on a retry, which is the cheap and
    correct thing to do.

    Substituting the module's `workflow` handle rather than driving a server, the same way
    `tests/test_publish.py` does: the real workflow API refuses to run outside a workflow event
    loop, and the function under test is the real, unmodified `_run_step`.
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

    async def _dispatch() -> None:
        for step in template.steps:
            await template_job.TemplateWorkflow()._run_step(
                step, {}, identity, timedelta(seconds=60)
            )

    asyncio.run(_dispatch())

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
    """A connector tool as `open_connector_specs` now produces one: an ordinary LangChain tool.

    That it is *ordinary* is the structural half of D-168's fix. There used to be two shapes on the
    assembled surface — in-process `FunctionTool`s and MAF's MCP wrappers — searched by two loops
    and called two ways, and the second way (`connector.call_tool`) reached the connector directly,
    skipping the audit trail and the authorization gate. With one shape there is no second path to
    tempt anyone, so the test can no longer plant a `call_tool` trap: there is nothing to trap.
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
    """Both tool steps of the shipped `hazard-briefing` used to leave no audit row at all.

    The in-process branch hand-applied audit + authz; the connector branch two lines below called
    `connector.call_tool` and reached the connector directly. The module's own docstring said
    applying them was the point of the module.
    """
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

    # **It raises**, and that is the difference between this caller and a chat turn. The chain's
    # two outermost middlewares convert a denial into prose a *model* can act on; a template step
    # has no model, and its result is interpolated into later steps — so a converted refusal would
    # become the step's `${steps.<id>.result}` and a later step would read "you are not authorized"
    # as though it were a hazard screening. `invoke_governed` therefore folds the governance half
    # only. The first version of this test asserted the converted text and passed; the job-step
    # tests are what caught it, because there the same conversion made a *refused* launch return a
    # payload and start the workflow.
    with pytest.raises(AuthorizationError):
        asyncio.run(_invoke([tool], _tool_step("screen_hazards", smiles=["CCO"]), []))

    assert calls == [], "the tool body ran despite the refusal"
    (event,) = sink.events
    assert event.outcome == "error", "a denied connector step left no audit row"


def test_a_step_result_is_something_temporal_can_carry() -> None:
    """MCP content blocks are not, and a step result crosses an activity boundary (D-168).

    Live, the shipped `hazard-briefing` template failed with "Unable to serialize unknown type" —
    after the missing worker registration was fixed and before this was — so no template with a
    `tool` step had ever completed a run. The offline tests could not see it: they call the
    activity in-process, where nothing serializes anything.

    Half of that failure was MAF's own envelope (`skip_parsing` and most of `_serializable` existed
    for it) and went with the framework. This is the half that did not: an MCP tool answers as
    content blocks on the wire whatever calls it.
    """
    from chemclaw.durable.template_activities import _mcp_text

    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert _mcp_text(blocks) == "a\nb"
    # Anything the converter already understands is handed through untouched.
    assert _mcp_text({"energy": -154.1}) == {"energy": -154.1}
    assert _mcp_text("plain") == "plain"


def test_a_structured_tool_result_is_not_mistaken_for_mcp_content() -> None:
    """`NoteRef` has a `type` field, and duck-typing on that flattened it to a repr string.

    The first version of this asked `hasattr(item, "type")`. `find_notes` returns `list[NoteRef]`,
    whose `type` is the note's *kind* — so the check matched, found no `.text`, and replaced a
    perfectly serializable structured result with `str(...)`. Silently, for every template step
    naming such a tool. The check is now "a list of dicts carrying a `type` key", which a list of
    pydantic models cannot satisfy however its fields are named.
    """
    from chemclaw.agent.graph_tools import NoteRef
    from chemclaw.durable.template_activities import _mcp_text

    notes = [NoteRef(id="reaction-1", type="reaction", source="eln", confidence=0.9)]
    assert _mcp_text(notes) is notes, "a structured result was flattened into a string"
