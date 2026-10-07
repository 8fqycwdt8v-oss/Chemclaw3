"""What one step hands the next, on every path the agent can drive.

Each path passes an address rather than routing data through the model's token stream: the
durable envelope, the deterministic template, and recovery after compaction clears a result.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from chemclaw.agent.compaction import _record_reduction
from chemclaw.agent.repeat_guard import begin_call_watch, count_call, end_call_watch
from chemclaw.api.runner import _job_results_message
from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.specs import ComplexJobSpec, EnsembleJobSpec, ScanJobSpec
from chemclaw.connectors.calc.workflows import job_envelope
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobResult
from chemclaw.science.calc.models import (
    Conformer,
    ConformerEnsemble,
    Structure,
)
from chemclaw.templates.manifest import JobStep, Template, ToolStep
from chemclaw.templates.resolve import UnresolvedReference, resolve


def _run(coroutine: Any) -> Any:
    """Run one coroutine to completion."""
    return asyncio.run(coroutine)


def _ensemble() -> ConformerEnsemble:
    """A two-member ensemble carrying real geometries, as a CREST search returns one."""
    from tests.calc_server_fake import embed as fake_embed

    structure = Structure.model_validate(fake_embed("CCO"))
    return ConformerEnsemble(
        smiles="CCO",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent=None,
        temperature_k=298.15,
        conformers=[
            Conformer(relative_kcal=0.0, population=0.7, degeneracy=1, structure=structure),
            Conformer(relative_kcal=0.9, population=0.3, degeneracy=2, structure=structure),
        ],
        total_found=47,
        conformational_entropy_cal_per_mol_k=1.2,
        ensemble_correction_kcal=-0.36,
    )


# --- the durable envelope ------------------------------------------------------------------------


def test_the_job_envelope_carries_addresses_and_not_coordinates() -> None:
    """The job envelope carries addresses, not coordinates.

    Tested as a pure function, since `CalcJobWorkflow` applies it in workflow code that must replay
    byte-identically.
    """
    envelope = job_envelope(
        XtbJobResult(kind="ensemble", summary="conformers of CCO", ensemble=_ensemble())
    )
    data = envelope.data

    members = data["conformers"]
    assert [member["structure"]["geometry_omitted"] for member in members] == [True, True]
    # The populations, the degeneracies and the entropy are the answer and survive whole.
    assert [member["population"] for member in members] == [0.7, 0.3]
    assert data["total_found"] == 47
    assert data["conformational_entropy_cal_per_mol_k"] == 1.2
    # `data` is the ensemble itself, not a wrapper around it: the bookkeeping fields the envelope
    # used to nest under stay on `ConnectorJobResult`, where core already reads them, and the shape
    # the result store needs is what `payload_kind` can then name.
    assert "ensemble" not in data and envelope.payload_kind == "ConformerEnsemble"


def test_the_envelope_carries_the_calculations_a_note_would_cite() -> None:
    """The envelope carries `calc_refs`, so a note drafted from a run can cite its calculations.

    It rides on the envelope's own field, as a fact about the run rather than the bundle's result.
    """
    envelope = ConnectorJobResult(summary="done", calc_refs=["xtb.opt@v1:aaaa:bbbb"])
    assert envelope.calc_refs == ["xtb.opt@v1:aaaa:bbbb"]
    # Additive and defaulted, so a payload decoded from an in-flight history still validates.
    assert ConnectorJobResult(summary="done").calc_refs == []


def test_a_real_job_run_collects_the_calculations_it_rested_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real job run collects the calculations it rested on.

    Driven through the activity, since a correct envelope shape says nothing about whether a run
    records keys.
    """
    from temporalio import activity

    from chemclaw.connectors.calc import activities
    from chemclaw.science.calc.store import InMemoryStore
    from tests.calc_server_fake import FakeCalcServer, install

    install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(activities, "default_store", InMemoryStore)
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)

    result = _run(activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO")))

    assert result.calc_refs, "a run that reached a keyed calculation recorded none"
    assert all(ref.startswith("xtb.") for ref in result.calc_refs)
    # De-duplicated: a key reached twice is one citation.
    assert len(set(result.calc_refs)) == len(result.calc_refs)


def test_the_mid_turn_resume_hands_the_model_json() -> None:
    """A Python `repr` — single quotes, `None`, `True` — on the busiest handoff there is.

    `templates/resolve._text` states the rule this now follows, in its own words: JSON "rather than
    a Python repr with single quotes that a model has to guess at".
    """
    message = _job_results_message(
        {"job-1": {"status": "completed", "result": {"kind": "ensemble"}, "summary": None}}
    )
    assert '"status": "completed"' in message
    assert "'status'" not in message
    assert "None" not in message


# --- the deterministic path ------------------------------------------------------------------


def test_a_template_can_chain_a_field_out_of_a_job_result() -> None:
    """A template can chain a field out of a job result with a dotted reference.

    Otherwise a computed value would have to pass through an `agent` step and the model.
    """
    envelope = ConnectorJobResult(
        summary="conformers of CCO",
        data={"ensemble": {"conformers": [{"structure": {"structure_id": "st_abc"}}]}},
    )
    scope = {"steps.search.result": envelope}

    # A whole-string reference yields the value with its type, at any depth.
    assert resolve("${steps.search.result.summary}", scope) == "conformers of CCO"
    assert resolve("${steps.search.result.data.ensemble}", scope) == envelope.data["ensemble"]
    # And an embedded one interpolates JSON, on a pydantic model as much as on a mapping.
    rendered = resolve("what happened: ${steps.search.result}", scope)
    assert '"summary": "conformers of CCO"' in rendered
    assert "summary='conformers of CCO'" not in rendered


def test_a_field_a_step_result_does_not_have_is_refused() -> None:
    """A reference that cannot resolve raises rather than yielding nothing.

    The module's whole safety argument: a reference that quietly became `None` would put a null
    into a calculation and produce a confident wrong answer.
    """
    scope = {"steps.search.result": ConnectorJobResult(summary="done")}
    with pytest.raises(UnresolvedReference, match="not a field"):
        resolve("${steps.search.result.nope}", scope)


def test_a_dotted_reference_is_validated_against_the_step_that_produced_it() -> None:
    """A dotted reference must name an earlier step; whether the field exists is a run-time fact."""
    Template.model_validate(
        {
            "name": "chained",
            "summary": "s",
            "inputs": [{"name": "smiles", "type": "string", "description": "d"}],
            "steps": [
                {"id": "search", "kind": "tool", "tool": "t", "arguments": {}},
                {
                    "id": "next",
                    "kind": "tool",
                    "tool": "u",
                    "arguments": {"x": "${steps.search.result.data.structure_id}"},
                },
            ],
        }
    )
    with pytest.raises(ValueError, match="not the result of an earlier step"):
        Template.model_validate(
            {
                "name": "forward",
                "summary": "s",
                "steps": [
                    {
                        "id": "first",
                        "kind": "tool",
                        "tool": "t",
                        "arguments": {"x": "${steps.later.result.a}"},
                    },
                    {"id": "later", "kind": "tool", "tool": "u", "arguments": {}},
                ],
            }
        )


# --- the calc job specs ------------------------------------------------------------------------


def test_every_geometry_taking_job_spec_accepts_a_handle() -> None:
    """The three durable calc jobs whose server primitive takes a `Structure`.

    `predict_site_reactivity` is deliberately absent: the calculation server has no
    `compute_fukui_at`, so a `structure_id` there would be a promise this repository cannot keep.
    """
    assert "structure_id" in ScanJobSpec.model_fields
    assert "structure_id" in EnsembleJobSpec.model_fields
    assert {"structure_id_a", "structure_id_b"} <= set(ComplexJobSpec.model_fields)


def test_a_half_specified_complex_pair_is_refused() -> None:
    """One chosen conformer against one fresh embedding is not a comparison of the two."""
    with pytest.raises(ValueError, match="together or not at all"):
        ComplexJobSpec(smiles_a="CCO", smiles_b="O", structure_id_a="st_a")


# --- context recovery --------------------------------------------------------------------------


def _reduced_request(cleared: str, args: dict[str, Any]) -> Any:
    """A request whose reduction cleared exactly one tool's result, as the middleware leaves it.

    Real messages with upstream's `context_editing.cleared` stamp, which `_cleared_calls` reads.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    call_id = "cleared-call"
    messages = [
        HumanMessage("go"),
        AIMessage("", tool_calls=[{"name": cleared, "args": args, "id": call_id}]),
        ToolMessage(
            "[cleared]",
            tool_call_id=call_id,
            response_metadata={"context_editing": {"cleared": True, "strategy": "clear_tool_uses"}},
        ),
    ]

    class _Request:
        """The two fields `_record_reduction` reads, as `ModelRequest` presents them."""

        state = {"messages": [*messages, HumanMessage("x " * 400)]}

    request = _Request()
    request.messages = messages  # type: ignore[attr-defined]
    return request


def test_a_compacted_turn_may_read_a_cleared_result_again() -> None:
    """A compacted turn may read a cleared result again.

    Compaction replaces old results with a placeholder, so an identical call after a reduction is a
    re-read, not a repeat.
    """
    token = begin_call_watch()
    try:
        for _ in range(settings.max_identical_tool_calls):
            assert count_call("gather_evidence", {"q": "x"}) is None
        assert count_call("gather_evidence", {"q": "x"}) is not None

        _record_reduction(_reduced_request("gather_evidence", {"q": "x"}))

        assert count_call("gather_evidence", {"q": "x"}) is None
    finally:
        end_call_watch(token)


def test_a_reduction_forgives_only_the_calls_whose_results_it_cleared() -> None:
    """A reduction forgives only the calls whose results it cleared.

    Clearing keeps the newest results. Asserted at the handoff, since the seam between the repeat
    guard and compaction is what is under test.
    """
    token = begin_call_watch()
    try:
        for _ in range(settings.max_identical_tool_calls):
            count_call("gather_evidence", {"q": "x"})
            count_call("find_past_jobs", {"q": "kept"})

        _record_reduction(_reduced_request("gather_evidence", {"q": "x"}))

        assert count_call("gather_evidence", {"q": "x"}) is None
        assert count_call("find_past_jobs", {"q": "kept"}) is not None
    finally:
        end_call_watch(token)


def test_a_turn_that_was_not_reduced_still_refuses_a_loop() -> None:
    """The measured behaviour the guard exists for — 7-8 identical calls in one turn — stands."""

    class _Request:
        state = {"messages": ["a", "b"]}
        messages = ["a", "b"]  # nothing reclaimed

    token = begin_call_watch()
    try:
        for _ in range(settings.max_identical_tool_calls):
            assert count_call("find_past_jobs", {}) is None
        _record_reduction(_Request())  # type: ignore[arg-type]
        assert count_call("find_past_jobs", {}) is not None
    finally:
        end_call_watch(token)


def test_the_resume_message_stays_framed_as_data() -> None:
    """Rendering changed; the injection discipline did not."""
    message = _job_results_message({"job-1": {"status": "completed"}})
    assert "retrieved-note" in message
    assert json.dumps({"status": "completed"}) in message


def test_the_shipped_refinement_template_carries_an_address_between_its_steps() -> None:
    """The shipped refinement template carries an address between its steps, with no model involved.
    """
    from pathlib import Path

    import yaml

    template = Template.model_validate(
        {
            "name": "conformer-refinement",
            **yaml.safe_load(
                Path("data/templates/conformer-refinement.yaml").read_text(encoding="utf-8")
            ),
        }
    )
    refine = next(step for step in template.steps if step.id == "refine")
    # `steps` is a `ToolStep | JobStep | AgentStep` union and only the first two carry `arguments`.
    # Asserted rather than cast: if this step ever becomes an agent step the test should say so
    # here, not fail three lines down on a missing attribute.
    assert isinstance(refine, ToolStep | JobStep)

    ensemble = _ensemble()
    # Built by the function the workflow itself calls, not assembled here to look like one. The
    # shipped template's reference is resolved against the envelope production actually produces,
    # so a change to that shape breaks this test rather than being discovered in the live lane.
    envelope = job_envelope(
        XtbJobResult(kind="ensemble", summary="conformers of CCO", ensemble=ensemble)
    )
    # Narrowed because `Template.steps` is a union and only the two capability steps carry
    # arguments; without it mypy reads `refine` as possibly an `AgentStep`.
    assert isinstance(refine, ToolStep | JobStep)
    resolved = resolve(
        refine.arguments,
        {"inputs.smiles": "CCO", "inputs.solvent": "", "steps.search.result": envelope},
    )

    # The address the search reported reaches the next step's argument, with its type, unmediated.
    assert resolved["structure_id"] == ensemble.conformers[0].structure.structure_id
    assert resolved["smiles"] == "CCO"
