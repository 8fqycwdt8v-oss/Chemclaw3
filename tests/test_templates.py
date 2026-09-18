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
    """A typo is not a literal string, and being strict about the *form* did not make it fail.

    `_REFERENCE` finds references; a span it cannot match is therefore not a bad reference but no
    reference at all, so every rule built on `references()` saw nothing to check. All six of these
    validated clean and `resolve` handed the tool the literal text — the same confident wrong
    answer as a null, with a stranger cause. `make template-validate` cannot catch it either: it
    checks argument *keys*, never values.
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
    """`${inputs.smiles} plus buffer` must interpolate, not silently drop " plus buffer".

    `_WHOLE` anchors `_REFERENCE` with `^...$` precisely so a reference embedded in a longer string
    falls through to `_REFERENCE.sub` instead of matching as a whole-string reference. Without the
    trailing `$` anchor, `re.match` still succeeds at position 0 and stops there, so `resolve` would
    return the referenced *value* alone (`"CCO"`) and drop everything typed after it — a step
    argument silently losing the text around its reference.
    """
    scope = {"inputs.smiles": "CCO"}
    assert resolve("${inputs.smiles} plus buffer", scope) == "CCO plus buffer"


def test_a_reference_with_leading_text_is_not_a_whole_string_match() -> None:
    """The mirror case: text before the reference must survive too.

    **It does not pin `_WHOLE`'s leading `^`, and reading it as if it does is the trap.** `_WHOLE`
    is used once, as `_WHOLE.match(value)`, and `re.match` anchors at position 0 whatever the
    pattern says — with no `re.MULTILINE`, `^` can never mean anything else. So deleting that `^`
    is an *equivalent* mutant: no input distinguishes the two, this test passes either way, and
    mutmut reporting it as a survivor would be a true statement about dead notation rather than
    about this suite. The trailing `$` is the anchor that does work, and
    `test_a_reference_with_trailing_text_is_not_a_whole_string_match` is what kills its mutant.
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

    **Framed rather than raw**, since the validation moved into `start_template_run` so both
    launchers share it: a raw `ValidationError` reaching a model is an unexpected-error result, and
    what a caller needs is the declared inputs by name. The refusal is asserted by what it says, so
    the message cannot quietly become useless while the test stays green.
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


def test_two_templates_generating_one_tool_name_fail_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tool_name` folds a hyphen to an underscore, so distinct files can claim one tool.

    Neither the name check nor the validator saw it, and the consequence is not a mis-run
    template: `register_tool` raises the first time the agent is built, so every turn fails with a
    message naming neither file. Driven here through `discovered`, which is where both files are
    still in hand.
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

    A bundle this release declares but does not run has no `connectors/<name>/server/tools.py`
    here, so its signatures are unresolvable and `step_problems` skips them — silently, by
    design, because an unresolvable tool must not produce invented failures. `hazard-briefing`
    calls `screen_hazards`, which made it the first shipped template that is name-checked and
    *not* argument-checked.

    The assertion is deliberately on the real shipped templates rather than a fixture: what would
    go wrong is not the reporting mechanism, it is somebody moving another bundle out and not
    noticing that a pinned procedure lost its argument check. This fails the moment that happens
    and the note stops matching what ships.

    **The blind spot grew from one template to five**, and pinning the whole set rather than a
    count is what makes that legible. Every addition is a `chem` enumeration — the bundle whose
    capability is `Chemclaw3-mcp`'s — so the multi-step protocols of
    `D-2026-08-25-the-loop-is-a-composite-not-a-template` are name-checked here and
    argument-checked only by `make live-template-args` against a running server. That is the known
    cost of enumerating on a bundle we declare and do not run, stated rather than discovered.

    That sentence used to name `make connector-validate`, which never dials a server: its rule
    imports the bundle's *in-tree* `server/` module and returns `[]` for exactly the bundles that
    ship none, so the gate named as the control here was the one structurally incapable of seeing
    this blind spot (`D-2026-08-29-connector-validate-never-dials-a-server`). `live-template-args`
    is the check that does open the session.
    """
    from chemclaw.cli.validate_templates import unchecked_arguments

    assert unchecked_arguments() == {
        "bond-strength-survey": ["enumerate_bond_cleavages"],
        "degradant-triage": ["enumerate_degradants", "screen_hazards"],
        "hazard-briefing": ["screen_hazards"],
        "microspecies-profile": ["enumerate_protonation_states"],
        "stereoisomer-ranking": ["enumerate_stereoisomers"],
        "tautomer-resolution": ["enumerate_tautomers"],
    }


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

    `resolvable_signatures()` reads `registered_tools()`, which is populated only as an import
    side effect of the agent package — and that import was supplied by `step_problems` happening
    to call `available_tools()` two lines earlier. Measured in a fresh interpreter before the fix:

        resolvable_signatures() alone    -> 30 signatures, 31 advertised tools uncovered
        available_tools() first, then it -> 50 signatures, 11 uncovered

    So reordering those two lines, or calling the function from anywhere else, silently dropped 20
    in-process tools from the check and the validator still printed "template validation passed" —
    a gate that quietly checks less is the exact failure mode `make template-validate` exists to
    close for templates. Run in a subprocess because the registry cannot be un-populated once this
    test session has imported the agent for something else.
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
    """The other way into the coverage loss the test above measures — and it was still open.

    `resolvable_signatures` caught every `ImportError`, so a bundle whose dependency stack is
    missing or renamed was indistinguishable from `qm`, which legitimately has no server module.
    Measured on this tree with one missing import injected into a connector's server tools module:
    50 signatures became 46 and `make template-validate` printed "template validation passed" and
    exited 0, while `make connector-validate` named the same bundle as broken. Two gates, one
    situation, opposite answers — so the import is now one shared function that raises, and this
    pins the raising half.
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

    @activity.defn(name="completed_steps")
    async def fake_completed_steps(request: Any) -> dict[str, Any]:
        """Stand in for the resume read, which wants a record store this test does not configure.

        Registered by the name the workflow dispatches, and answering `{}` — nothing to resume —
        which is what a first run of any id gets. Without it the run stalls on an activity nothing
        serves, exactly as the record write below does.
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
                # **The record write needs a worker, or this test measures a timeout.**
                # `TemplateWorkflow` ends by dispatching `record_job` to the background queue, and
                # nothing here served it — so the run only finished when that activity's bound
                # expired, and the assertions below ran 60 s later than they read. That was
                # invisible while the bound was a 60 s `schedule_to_close`; splitting it into a
                # 900 s `schedule_to_start` turned the same 63-second test into a fifteen-minute
                # one and stalled the suite. A test whose duration is somebody else's timeout is
                # measuring the timeout.
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
    assert event.outcome == "refused", "a denied connector step left no audit row"


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


def test_a_tool_steps_structured_content_reaches_the_next_step_as_a_model() -> None:
    """The defect that made four shipped templates die on their second step, pinned end to end.

    `run_tautomer-resolution` and three siblings hand one tool's field to the next step
    (`species: "${steps.forms.result.smiles}"`). That reference raised `UnresolvedReference` at
    *run* time — after the launch, inside the workflow — because `_mcp_text` had already joined the
    content blocks into a string, so `smiles` was being asked of a `str`. CI was green throughout:
    `make template-validate` checks that the step ids resolve backwards and that the tool exists,
    never that the result has the shape the reference walks.

    This asserts the whole path rather than `_structured` alone, because the bug lived in the seam
    between two correct functions: `_mcp_text` flattens content, which is right for text, and
    `ainvoke(args)` returns content only, which is right for a chat turn. Only the composition was
    wrong, so only a test over the composition can hold it.
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
    """A green line over zero templates is a gate that reported on nothing.

    `discovered()` returning `{}` yielded no problems, `enabled()` over an empty list yielded none
    either, and the green line printed — for an empty directory and for one that does not exist at
    all. The nine shipped templates are what back the `run_*` launcher tools the agent advertises,
    so an image that failed to ship `data/templates/`, or a mis-set `CHEMCLAW_TEMPLATES_DIR` in a
    container, is precisely the condition this gate would be expected to catch and the one it could
    not see. Both sibling seams already refuse an empty discovery in these words.
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
# `make template-validate` cannot answer for a bundle this repository declares and does not run —
# there is no local signature to read, and seven shipped steps are in that state. The live gate
# (`make live-template-args`) answers from a running server's advertised schema instead. What is
# testable offline is its *judgment*: given the tools a session did and did not produce, does it
# check what it can, refuse to invent what it cannot, and say which is which. The reaching itself
# is the live lane's job, and the run is recorded in
# `docs/decisions/D-2026-08-27-an-argument-check-needs-a-live-session.md`.


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
    """A harness that reached two of five servers checked two — D-2026-08-17, as a return value.

    A connector that did not come up contributes no tools, so every check against it would pass
    vacuously. It is recorded as *unreached* instead: not a problem (nothing is known to be wrong)
    and not a pass (nothing was looked at). This is the assertion that keeps the green line honest,
    and `main` turns the same distinction into a distinct exit code.
    """
    from chemclaw.cli.validate_template_args_live import check_live_arguments

    report = check_live_arguments(
        [_live_template(smilez=["CCO"])], _OWNERS, {}, unreachable=("safety",)
    )
    assert report.problems == []
    assert report.checked == []
    assert report.unreached == {"safety": ["probe/one -> screen_hazards"]}


def test_the_live_check_flags_a_tool_a_reachable_server_does_not_serve() -> None:
    """A manifest declaring what its server does not answer is a template that fails at the call.

    Both `connector.yaml` files for these bundles say in prose that the two copies of the tool list
    can drift and that only a running server settles it. Offline nothing can: the local check has
    no module to read. Here the connector came up, so its silence about the tool is evidence rather
    than absence, and it is reported as a problem rather than skipped.
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
    """The two authorities must agree wherever both can answer, or the lanes are two rules.

    `ToolArguments` exists to make that structural rather than hoped for: the offline gate builds
    one from an `inspect.Signature` and the live gate from the schema a session advertised, and
    `argument_problems` is the single reader. This pins the two constructors against one function
    and its served form — the case where a disagreement would be a real defect and silent.
    """
    import inspect

    from chemclaw.cli.validate_template_args_live import _live_arguments
    from chemclaw.cli.validate_templates import ToolArguments

    async def screen_hazards(smiles: list[str], top_k: int = 5) -> str:
        """Screen a molecule for hazards."""
        return "hazard: none found"

    offline = ToolArguments.of_signature(inspect.signature(screen_hazards))
    live = _live_arguments(_live_tool("screen_hazards"))
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
    """A manifest the registry cannot load must still be *reported*, not raised through `main`.

    `main` resolves the tool surface before anything else, and `available_tools` asks the agent for
    its tool names, which asks this registry for the `run_*` launchers — so a template whose own
    manifest is invalid (an unknown `${inputs.x}`, a forward `${steps.y.result}`) fails inside the
    registry load rather than inside the step checker, and the operator got a pydantic traceback.
    The exit code was already 1, so CI was never misled; `validate_kg.main` states the rest of the
    rule — "it must still fail; it must not fail *looking like a crash*".
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
    """A duplicate launch is the idempotency contract succeeding, and it has to be *said*.

    This branch returned the id and told nobody: no `JobSignal` reached the turn, `started_jobs`
    stayed empty, `agent/job_results.py` had nothing to wait on, and the second chemist to ask for
    a running template — or the same one re-asking — was told "in progress" with no row that a
    later `job_completed` could clear. `connectors/jobs.py` documents having fixed exactly this for
    jobs; the template launcher is that launcher minus the fix.
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
    """`RUNNING`, not "not completed" — the distinction the announcement rests on.

    A finished, failed or cancelled run will never emit the `job_completed` that clears an
    announced row, so announcing one would draw a row nothing takes away. The id still comes back:
    the rejoin succeeded either way.
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
    """`connect()` frames an unreachable broker; this is the call *after* it, which did not.

    A queue with no worker, a transient RPC timeout or a serialization error escaped raw, and
    `agent/tool_authz.surface_domain_errors` classifies an `RPCError` as neither a `ChemclawError`
    nor a transport failure — so the model was handed `unexpected_error_result()` about a template
    that may or may not have started. The sibling launcher frames it as a `ConnectorJobError` with
    the check to run before relaunching, and that is what a template must say too.
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
    """A launcher must not start a procedure into a fleet that cannot run it.

    `make template-validate` has always known this — at a deployment with no `calc` bundle it
    reports "runs unknown job 'survey_bond_strengths'; declared jobs: []" and exits 1 — and
    nothing at run time consulted it. So `run_bond_strength_survey` started `TemplateWorkflow`,
    returned an id, and the system prompt told the model to report that id as work in progress and
    poll it; `find_past_jobs` then found nothing, because a run that never reaches a step writes no
    record.

    The refusal reuses the gate's own `step_problems` rather than restating the rule — two copies
    of "what resolves" is the defect class this repository keeps finding — and it happens **before**
    the client is dialled, so the promise that nothing was queued is one the launcher can keep.
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

    `chained` decides whether each step reads the one before it, which is the whole difference the
    ceiling turns on now that the sequencer schedules waves: chained steps are N waves and cost N
    ceilings, independent ones share a wave and cost one. Defaulting to chained keeps these
    fixtures expressing the case the bound was written for — a procedure that genuinely runs its
    jobs one after another.
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
    """The bound `core/config` cannot state, because it cannot see `data/templates/`.

    `_the_template_run_ceiling_covers_one_step` checks the run ceiling against the longest *single*
    step — the honest machine-checkable floor for an object holding no YAML. Measured on the
    shipped defaults, one `job` step is 39,330 s against a run ceiling of 45,330 s, so that
    validator passes and **two** of them in one file miss by 33,330 s.

    What the gap costs is why this is a gate and not a note: a workflow *execution* timeout is not
    delivered to workflow code, so `TemplateWorkflow`'s `except BaseException -> _notify_failure`
    never runs. No failure row, no push-back, nothing on the session stream — the run simply stops.
    Every other way a template can fail says so somewhere.
    """
    problems = run_ceiling_problems(_template(steps=_job_steps(2)))

    assert len(problems) == 1
    assert "cannot finish inside template_run_timeout_seconds" in problems[0]
    # The arithmetic, not just the verdict: an operator reading this has to know which step to
    # shorten, and "the template is too long" is unactionable over a procedure with five of them.
    assert "j0=39,330s" in problems[0]
    assert "j1=39,330s" in problems[0]


def test_the_refusals_printed_terms_add_up_to_the_printed_total() -> None:
    """A message explaining a ceiling must not invite arithmetic that contradicts its own total.

    A wave's members were joined with `" + "`, which reads as addition and is wrong for steps that
    run together: a two-`job` wave printed `survey=39,330s + survey2=39,330s` beside a total that
    counted one of them, so a reader adding the printed numbers got 80,460 where the message said
    79,560. Concurrent members are `" | "` inside brackets carrying the wave's own cost; a wave of
    one prints as the bare step it is. Driven by adding up what the message actually prints.
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
    """A wave's ceiling is its slowest member *per batch*, not once however wide it is.

    The old arithmetic sized any wave at one slow step, which is only true if every member is
    really in flight together — and no worker promises that. Measured before this: 501 independent
    `tool` steps passed the run ceiling as if the whole procedure cost 900s. A reviewed file's bound
    is its reviewer; an agent-authored one reaches this arithmetic with nobody having looked.

    The number that bounds it is the number that sizes it: `TemplateWorkflow._run_wave` runs a wave
    in batches of `TemplateRunInput.max_parallel_steps`, pinned at launch from this same setting.
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
    """The cost model, which is the half a shared `limit` does not make shared.

    Sizing a wave as `ceil(width / limit) x the whole wave's slowest member` charges the slow step
    to every batch, including batches holding nothing slow — and that form passed every test here,
    because the ones that existed used waves narrow enough to fit in one batch. Driven with a
    document where the two disagree: one `job` step (39,330 s) beside eight `tool` steps (900 s)
    is one wave of nine, two batches, so it costs 39,330 + 900 and not 2 x 39,330.

    An over-stating bound is not the conservative choice it looks like: it refuses a procedure that
    would have finished. This one fits its run ceiling with 4,200 s to spare and was refused by
    34,230 s.
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
    """**The enforcing half**, which was asserted nowhere while the checking half was.

    `run_ceiling_problems` sizes a wave by the batches `_run_wave` will dispatch, so a `_run_wave`
    that ignored the bound would make the whole arithmetic a wish. Driven: mutating the dispatch
    loop to `for batch in (wave,)` — restoring the unbounded gather this exists to stop — left every
    template, workflow-replay and composed-workflow test green, which is this repository's own
    definition of a claim that a control exists.

    Driven against `_run_wave` directly with an injected `_run_step`, because the alternative is a
    Temporal environment per case and what is under test is the dispatch shape, not the broker.
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
    """**The other enforcing half**: the run carries the number, it does not read it later.

    Driven, because mutating `max_parallel_steps=settings.orchestrator_max_parallel_children` to
    `0` — every run unbounded — left the template, composed-workflow and API suites green. A live
    settings read inside workflow code would be nondeterministic on replay *and* would not be the
    value `run_ceiling_problems` sized the launch with; pinning it is what makes them one number.
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
    """The bound follows the schedule, and this is the case where that is the whole difference.

    Two `job` steps cost two ceilings when one reads the other and **one** when neither does,
    because `templates/schedule.py` puts independent steps in the same wave. A flat sum would
    refuse the second template below — a procedure that would have finished well inside its run
    ceiling — which is why an over-stating bound is not the conservative choice it looks like.
    """
    assert run_ceiling_problems(_template(steps=_job_steps(2, chained=False))) == []
    assert run_ceiling_problems(_template(steps=_job_steps(2, chained=True))) != []


@pytest.mark.parametrize("name", sorted(registry.discovered()))
def test_every_shipped_template_fits_this_deployments_run_ceiling(name: str) -> None:
    """Latent rather than live, which is exactly when a bound is worth adding.

    No shipped template has two `job` steps — the catalogue measures 2,700 s to 41,130 s against
    45,330 s — so this passes today and is here for the first template that deepens one. Per
    template rather than over the set, so a failure names the file.
    """
    assert run_ceiling_problems(registry.discovered()[name]) == []


def test_the_launcher_refuses_a_run_the_ceiling_cannot_hold_before_anything_is_queued() -> None:
    """The gate's answer and the launcher's are one function, so they cannot drift apart.

    `unrunnable_reason` already refused a template whose steps do not *resolve* at this deployment.
    Timing is the same kind of fact — a deployment property that makes the launch pointless — and
    it fails more quietly, so it is the better of the two to catch before a workflow id exists.
    """
    blocked = registry.unrunnable_reason(_template(steps=_job_steps(2)))

    assert "cannot finish inside template_run_timeout_seconds" in blocked


def test_the_config_floor_and_the_template_gate_read_one_step_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two readers, one definition — the property that keeps the two bounds from disagreeing.

    `core/config` asks this for the *maximum* (does one step fit?) and `run_ceiling_problems` for
    the *sum over a file's steps* (does the procedure fit?). Both questions, one arithmetic.

    Checked by moving the setting the `job` ceiling is built from and watching the gate's answer
    move with it, rather than by asserting a transcribed number — the count of post-child steps in
    that sum was six, then it was not, and a test quoting the total would have gone stale with it.
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

    Each `tool` step sleeps `_STEP_SECONDS` and records when it entered and left. **Wall clock, not
    a call count**: whether two activities were dispatched together is exactly the thing a count
    cannot see, and the defect this guards — concurrency silently lost to a future edit of the
    sequencer — would leave every count unchanged.
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

        Registered by the name the workflow dispatches, and answering `{}` — nothing to resume —
        which is what a first run of any id gets. Without it the run stalls on an activity nothing
        serves, exactly as the record write below does.
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
                    # Unique per probe: a Temporal id is an idempotency key, so two probes
                    # sharing one would have the second rejoin the first's finished run and
                    # measure nothing. Which is exactly what happened when this derived the id
                    # from the step shape — both probes have three steps starting at "a".
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
    """The headline. Two independent `tool` steps, measured overlapping rather than counted.

    **Nothing in the file asked for this**, which is the design: concurrency is derived from the
    `${steps.<id>.result}` edges the template already declares, so a procedure gets it by not
    stating a dependency it never had. Measured over the shipped catalogue, two of the nine —
    `degradant-triage` and `hazard-briefing` — were already shaped this way and were being run one
    after the other for no reason anybody had written down.
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
    """The control arm, and the one that matters most: a chain must not gain concurrency.

    Seven of the nine shipped templates chain, and a scheduler that ran their steps together would
    hand a calculation a `${steps.<id>.result}` that does not exist yet — the failure mode the
    forward-reference validator exists to make impossible at load time.
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

    `resume_from` is what the resume read answers with — the shape a real `job_records` row holds,
    `{"steps": ..., "template_fingerprint": ...}` — so this drives the sequencer's own decision
    about what to skip rather than re-testing the store.
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
    """The headline: the work `failed_template_record` kept is the work the next attempt skips.

    Before this, `scope` and `results` were rebuilt empty on every execution, so a procedure that
    died at step four redid all four — while its own `job_records` row held their results under a
    docstring explaining why discarding them would be wrong.
    """
    ran, run = _resumable_run(fail_on=set(), resume_from={"one": {"ok": "two"}})

    result = asyncio.run(run())

    # Step one never ran: its result came from the record. Step two ran, and read what step one
    # produced on the attempt that did run it.
    assert ran == ["two", "three"], ran
    assert result.steps["one"] == {"ok": "two"}
    assert result.result == "final"


def test_a_resume_is_refused_when_the_template_has_changed_under_the_same_id() -> None:
    """The guard, and it is the reason this is not simply a cache.

    A run's id is `hash([name, inputs])` and says nothing about the steps, so editing the file and
    relaunching lands on the same id carrying a different procedure. Folding the old run's step
    results into it would mix two definitions silently — the failure mode that makes a wrong answer
    rather than a slow one.
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
    """The collision that returned somebody else's finished work, driven.

    A run id is an idempotency key and the launcher *rejoins* an id already started. For a
    `data/templates/` file that is right: the name is the procedure, reviewed and the same for
    everybody, so two people asking the same question share one run
    (`D-2026-08-01-a-running-job-has-no-owner`). A composed workflow breaks both halves — the name
    is one chemist's, and its steps change when they re-compose.

    Measured before `scope` existed: two documents with nothing in common but the name `triage`
    both produced `template-triage-65f5e26304a2c36c`, so running the second returned the first's
    completed result and its step outputs, under a summary that reads correct. A second chemist was
    denied their own workflow for as long as the first's run was retained.
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
    """The control arm, and it is why `scope` defaults to empty rather than to something.

    A file template's id appears in archived histories, in Temporal's own retention and in tests.
    Changing it would orphan every in-flight run and every fixture, to fix a collision that cannot
    happen for a document whose name *is* its identity.
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
