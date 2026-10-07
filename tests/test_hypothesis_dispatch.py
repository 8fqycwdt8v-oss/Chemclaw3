"""Refusing to run a discriminating check unless every argument is grounded.

The policy: nothing a model writes becomes a tool argument. Most tests are refusals, each naming a
way a check could otherwise reach a calculator with an unchecked value.
"""

import inspect
from typing import Any

import pytest

from chemclaw.agent.template_surface import ToolArguments, resolvable_signatures
from chemclaw.hypotheses.dispatch import (
    STRUCTURE_ARGUMENT,
    Dispatch,
    Refusal,
    Sweep,
    ToolContract,
    contract_of,
    defaulted_arguments,
    refuse_unless_dispatchable,
    structure_of,
)
from chemclaw.kg.note import Note


def _contract(
    *, required: set[str], accepted: set[str] | None = None, kind: str = "string"
) -> ToolContract:
    return ToolContract(
        required=frozenset(required),
        accepted=frozenset(accepted or ()) | frozenset(required),
        structure_type=kind,
    )


# ------------------------------------------------------------------ what may be dispatched


def test_a_structure_only_tool_is_dispatchable() -> None:
    assert refuse_unless_dispatchable("predict_pka", _contract(required={"smiles"})) is None


def test_optional_arguments_do_not_block_dispatch() -> None:
    """They are never supplied, so they cannot be invented — they are disclosed instead."""
    contract = _contract(required={"smiles"}, accepted={"solvent", "charge"})
    assert refuse_unless_dispatchable("compute_xtb_energy", contract) is None
    assert defaulted_arguments(contract) == ("charge", "solvent")


def test_a_tool_needing_a_second_argument_refuses() -> None:
    """The whole point. A model filling `solvent` in would be inventing the answer's conditions."""
    refusal = refuse_unless_dispatchable(
        "compute_in_solvent", _contract(required={"smiles", "solvent"})
    )
    assert refusal is not None
    assert refusal.code == "needs-more-than-a-structure"
    assert "solvent" in refusal.detail


def test_a_tool_that_is_not_about_one_compound_refuses() -> None:
    refusal = refuse_unless_dispatchable("find_calculations", _contract(required={"query"}))
    assert refusal is not None
    assert refusal.code == "not-a-structure-tool"


def test_a_structure_argument_that_is_not_a_string_refuses() -> None:
    """A structure argument that is not a string refuses.

    `screen_hazards` takes a list of SMILES; a check's subject is a SMILES string, so the type is
    checked as well as the keys.
    """
    refusal = refuse_unless_dispatchable(
        "screen_hazards", _contract(required={"smiles"}, kind="array")
    )
    assert refusal is not None
    assert refusal.code == "structure-is-not-a-string"


def test_an_unreadable_contract_refuses_rather_than_passing() -> None:
    """An unreadable contract refuses rather than passing.

    Unlike a driver, a calculator does not refuse a bad argument; it returns a plausible number, so
    permissiveness is the defect here.
    """
    refusal = refuse_unless_dispatchable("mystery", ToolContract(readable=False))
    assert refusal is not None
    assert refusal.code == "unreadable-contract"


def test_an_absent_tool_refuses_and_says_so() -> None:
    refusal = refuse_unless_dispatchable("not_here", None)
    assert refusal is not None
    assert refusal.code == "tool-unavailable"


def test_a_tool_that_is_not_on_the_surface_refuses() -> None:
    """A tool that is not on the surface refuses, which is also what an empty name reduces to.

    The production caller returns `no-call` earlier when the check named nothing.
    """
    refusal = refuse_unless_dispatchable("compute_moon_phase", None)
    assert refusal is not None
    assert refusal.code == "tool-unavailable"


def test_a_tool_whose_default_is_wrong_for_the_molecule_refuses() -> None:
    """A tool whose default is wrong for the molecule refuses.

    Arity is not sufficient: `compute_thermochemistry`'s `symmetry_number` defaults to 1, which is
    false for symmetric molecules and shifts the free energy by `R·ln(sigma)` with no sign in the
    result.
    """
    refusal = refuse_unless_dispatchable(
        "compute_thermochemistry", _contract(required={"smiles"}, accepted={"symmetry_number"})
    )
    assert refusal is not None
    assert refusal.code == "unsafe-defaults"
    assert "symmetry_number" in refusal.detail


# ------------------------------------------------------------------ grounding the subject


def _note(note_id: str, note_type: str, smiles: str | None) -> Note:
    return Note(id=note_id, type=note_type, compound_smiles=smiles, body="x")


def test_a_resolved_compound_yields_its_own_structure() -> None:
    """The structure comes off the note, so the model never writes one."""
    smiles, refusal = structure_of(
        _note("compound-aspirin", "compound", "CC(=O)Oc1ccccc1C(=O)O"), "compound-aspirin"
    )
    assert refusal is None
    assert smiles == "CC(=O)Oc1ccccc1C(=O)O"


def test_an_id_that_does_not_resolve_refuses() -> None:
    """A model naming a note from memory is the case this whole path exists for."""
    _, refusal = structure_of(None, "compound-invented")
    assert refusal is not None
    assert refusal.code == "subject-not-found"


def test_a_subject_that_is_not_a_compound_refuses() -> None:
    _, refusal = structure_of(_note("reaction-1", "reaction", None), "reaction-1")
    assert refusal is not None
    assert refusal.code == "subject-not-a-compound"


def test_a_compound_with_no_structure_refuses() -> None:
    _, refusal = structure_of(_note("compound-x", "compound", None), "compound-x")
    assert refusal is not None
    assert refusal.code == "subject-has-no-structure"


def test_a_structure_rdkit_will_not_read_refuses() -> None:
    """A bare RDKit parse reads `"CCO junk"` as ethanol; this gate does not.

    `require_canonical_smiles` is the measured defect `core/chem.require_molecule` was written
    against, and it is the same gate the calculation cache keys on.
    """
    _, refusal = structure_of(_note("compound-x", "compound", "CCO junk"), "compound-x")
    assert refusal is not None
    assert refusal.code == "subject-structure-unparseable"


def test_the_call_is_the_structure_and_nothing_else() -> None:
    plan = Dispatch(tool="predict_pka", smiles="CCO", subject_note_id="compound-ethanol")
    assert plan.arguments == {STRUCTURE_ARGUMENT: "CCO"}


# ------------------------------------------------------------------ the ratchet


#: Every tool a check may be dispatched onto today. Pinned rather than counted, so a change is a
#: reviewed diff. A tool gaining a required argument drops out of this set by itself, and the
#: failure lands here rather than in a chemist's result.
_DISPATCHABLE = {
    "compute_atomic_descriptors",
    "compute_electronic_properties",
    "compute_surface_potential",
    "compute_xtb_energy",
    "optimize_geometry",
    "predict_developability_profile",
    "predict_logd",
    "predict_pka",
    "predict_site_reactivity",
    "predict_solubility",
    "similar_molecules",
    "substrate_precedent",
}


def _local_contract(signature: inspect.Signature) -> ToolContract:
    """A contract from a local signature, with the structure's type read off the annotation.

    The keys half is `ToolArguments`' definition rather than a fourth reading of the same question
    — see `dispatch.py` on why there is one.
    """
    accepts = ToolArguments.of_signature(signature)
    parameter = signature.parameters.get(STRUCTURE_ARGUMENT)
    kind = "string" if parameter is not None and parameter.annotation is str else ""
    return contract_of(
        {"properties": {STRUCTURE_ARGUMENT: {"type": kind}}} if kind else None, accepts
    )


#: Bundles whose servers live elsewhere, so no local signature can be introspected. `_DISPATCHABLE`
#: pins the locally readable tools, not the live surface, which `run_computable_check` judges from
#: each session's advertised schema. A new bundle joining this set turns the assertion below red,
#: which is the moment to read its tools' defaults.
_SERVED_ELSEWHERE = {"chem", "rxnpredict", "safety"}


def test_the_dispatchable_set_is_exactly_what_is_pinned() -> None:
    """Derived from live signatures, so it tracks the tools rather than a maintained list."""
    found = {
        name
        for name, signature in resolvable_signatures().items()
        if refuse_unless_dispatchable(name, _local_contract(signature)) is None
    }
    assert found == _DISPATCHABLE, (
        "the set of tools a hypothesis check may be dispatched onto has changed. A tool that "
        "gained a required argument correctly drops out — update the set. A tool that appeared "
        "needs its defaults read before it is added: arity does not catch a default that is wrong "
        "for the molecule (see `compute_thermochemistry`)."
    )


def test_the_tool_ratchets_blind_spot_is_declared() -> None:
    """The tool ratchet's blind spot is declared.

    `resolvable_signatures()` needs a local `connectors.<name>.server.tools` module, so other
    enabled bundles' tools are invisible to it; their set is asserted so a new one is a decision.
    """
    from chemclaw.connectors.registry import enabled, server_tools_module

    unreadable = {
        manifest.name
        for manifest in enabled()
        if manifest.endpoint
        and manifest.endpoint.tools
        and server_tools_module(manifest.name) is None
    }
    assert unreadable == _SERVED_ELSEWHERE, (
        "the set of bundles this repository declares and cannot introspect has changed. A bundle "
        "that joined it has endpoint tools a hypothesis check can be dispatched onto with no "
        "reviewer having read their defaults — read them, then add the name here."
    )


def test_every_excluded_tool_still_exists() -> None:
    """A deny-list outliving its tool is a rule about nothing, and reads as coverage."""
    from chemclaw.hypotheses.dispatch import _UNSAFE_DEFAULTS

    signatures = resolvable_signatures()
    missing = sorted(name for name in _UNSAFE_DEFAULTS if name not in signatures)
    assert not missing, f"excluded tools that no longer exist: {missing}"


def test_a_tool_excluded_for_its_defaults_would_otherwise_qualify() -> None:
    """Otherwise the exclusion is doing no work and the reason for it has gone stale."""
    from chemclaw.hypotheses.dispatch import _UNSAFE_DEFAULTS

    signatures = resolvable_signatures()
    for name in _UNSAFE_DEFAULTS:
        contract = _local_contract(signatures[name])
        assert contract.required == frozenset({STRUCTURE_ARGUMENT}), (
            f"{name} is excluded for its defaults, but it no longer qualifies on arity either — "
            "the exclusion is now redundant and its reason should be re-read before it is kept"
        )


@pytest.mark.parametrize("name", sorted(_DISPATCHABLE))
def test_every_dispatchable_tool_requires_only_the_structure(name: str) -> None:
    """Stated per tool so a failure names the one that changed."""
    signature = resolvable_signatures()[name]
    assert ToolArguments.of_signature(signature).required == frozenset({STRUCTURE_ARGUMENT})


# ------------------------------------------------------------------ durable jobs


def _job_fields(job_name: str) -> dict[str, tuple[bool, Any]]:
    """One shipped calc job's declared params, reduced to what `ground_job_params` reads."""
    from chemclaw.connectors.jobs import _params_model
    from chemclaw.connectors.registry import discovered

    _bundle, manifest = discovered()["calc"]
    job = next(spec for spec in manifest.jobs if spec.name == job_name)
    model = _params_model("calc", job)
    return {name: (f.is_required(), f.default) for name, f in model.model_fields.items()}


#: Jobs a tournament may launch on its own, derived from each job's params model. Jobs needing atom
#: indices or a cleavage list are absent: a model produces those plausibly and wrongly, and they
#: become dispatchable only when something enumerates them.
_DISPATCHABLE_JOBS = {
    "compare_solvents",
    "compute_ensemble_property",
    "compute_interaction_energy",
    "compute_reaction_energy",
    "predict_pka_ensemble",
    "rank_species",
    "rank_species_across_solvents",
    "refine_ensemble",
    "sample_conformers",
}


#: The sweep-width budget these tests ground against. A literal rather than the live setting: the
#: refusal is what is under test, not what a deployment happens to allow today.
_SWEEP_BUDGET = 6


def _fields_of(connector: str, job: Any) -> dict[str, tuple[bool, Any]]:
    """One job's declared params, reduced to what `ground_job_params` reads."""
    from chemclaw.connectors.jobs import _params_model

    model = _params_model(connector, job)
    return {
        name: (declared.is_required(), declared.default)
        for name, declared in model.model_fields.items()
    }


def _try_ground(job_name: str) -> tuple[dict[str, Any] | None, Refusal | None]:
    from chemclaw.hypotheses.dispatch import (
        STRUCTURE_FIELDS,
        SWEEPABLE_FIELDS,
        Sweep,
        ground_job_params,
    )

    fields = _job_fields(job_name)
    required = [name for name, (req, _) in fields.items() if req]
    subjects = {
        name: (["a"] if STRUCTURE_FIELDS[name] == "one" else ["a", "b"])
        for name in required
        if name in STRUCTURE_FIELDS
    }
    axis = next((name for name in required if name in SWEEPABLE_FIELDS), "")
    sweep = Sweep(parameter=axis, values=("thf",)) if axis else None
    return ground_job_params(fields, subjects, {"a": "CCO", "b": "CCN"}, sweep, _SWEEP_BUDGET)


def test_the_dispatchable_job_set_is_exactly_what_is_pinned() -> None:
    """Derived from the manifests' own params models, so it tracks the jobs."""
    from chemclaw.connectors.registry import discovered

    _bundle, manifest = discovered()["calc"]
    found = {job.name for job in manifest.jobs if _try_ground(job.name)[1] is None}
    assert found == _DISPATCHABLE_JOBS, (
        "the set of durable jobs a hypothesis check may launch has changed. A job that gained a "
        "field nothing in the record can supply correctly drops out. A job that appeared needs "
        "its required fields classified in `STRUCTURE_FIELDS`/`SWEEPABLE_FIELDS` before it is "
        "added — an unclassified field fails closed, which is the intent."
    )


@pytest.mark.parametrize(
    ("job_name", "invented"),
    [
        ("scan_coordinate", "atoms"),
        ("profile_rotation", "torsion"),
        ("survey_bond_strengths", "cleavages"),
    ],
)
def test_a_job_needing_a_value_only_a_model_could_supply_refuses(
    job_name: str, invented: str
) -> None:
    """Atom indices are the sharpest case: a model produces them fluently and they are wrong.

    Nothing in the resulting number says which atoms were driven, so a scan over the wrong pair
    reads exactly like a scan over the right one.
    """
    _, refusal = _try_ground(job_name)
    assert refusal is not None
    assert refusal.code == "field-cannot-be-grounded"
    assert invented in refusal.detail


def test_an_unclassified_field_fails_closed() -> None:
    """The property the curation rests on: omission makes a job undispatchable, never runnable."""
    from chemclaw.hypotheses.dispatch import ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None), "mystery": (True, None)},
        {"smiles": ["a"]},
        {"a": "CCO"},
        None,
        _SWEEP_BUDGET,
    )
    assert refusal is not None
    assert refusal.code == "field-cannot-be-grounded"


def test_a_swept_axis_must_have_a_vocabulary_behind_it() -> None:
    """Varying is allowed; varying over values nothing validates is not."""
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None)},
        {"smiles": ["a"]},
        {"a": "CCO"},
        Sweep("temperature_k", ("300",)),
        _SWEEP_BUDGET,
    )
    assert refusal is not None
    assert refusal.code == "axis-not-sweepable"


def test_a_sweep_over_no_values_refuses() -> None:
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None), "solvents": (True, None)},
        {"smiles": ["a"]},
        {"a": "CCO"},
        Sweep("solvents", ()),
        _SWEEP_BUDGET,
    )
    assert refusal is not None
    assert refusal.code == "axis-empty"


def test_a_subject_that_did_not_resolve_refuses_rather_than_dropping() -> None:
    """Silently shortening a reactant list would change the reaction being computed."""
    from chemclaw.hypotheses.dispatch import ground_job_params

    _, refusal = ground_job_params(
        {"reactants": (True, None)},
        {"reactants": ["a", "ghost"]},
        {"a": "CCO"},
        None,
        _SWEEP_BUDGET,
    )
    assert refusal is not None
    assert refusal.code == "subject-not-found"
    assert "ghost" in refusal.detail


def test_a_scalar_structure_field_refuses_several_subjects() -> None:
    from chemclaw.hypotheses.dispatch import ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None)},
        {"smiles": ["a", "b"]},
        {"a": "CCO", "b": "CCN"},
        None,
        _SWEEP_BUDGET,
    )
    assert refusal is not None
    assert refusal.code == "subject-arity"


def test_the_solvent_screen_grounds_into_a_call_the_job_accepts() -> None:
    """The scenario this whole extension is for, checked against the job's real params model."""
    from chemclaw.connectors.jobs import _params_model
    from chemclaw.connectors.registry import discovered
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    params, refusal = ground_job_params(
        _job_fields("compare_solvents"),
        {"reactants": ["a"], "products": ["b"]},
        {"a": "CCO", "b": "CC=O"},
        Sweep("solvents", ("thf", "dmf", "toluene")),
        _SWEEP_BUDGET,
    )
    assert refusal is None
    assert params is not None
    assert params["solvents"] == ["thf", "dmf", "toluene"]

    _bundle, manifest = discovered()["calc"]
    job = next(spec for spec in manifest.jobs if spec.name == "compare_solvents")
    # The declared params model is the authority, exactly as `prepare_job_launch` uses it.
    _params_model("calc", job).model_validate(params)


def test_a_sweep_wider_than_the_budget_refuses_rather_than_trimming() -> None:
    """A sweep wider than the budget refuses rather than trimming.

    The swept values are reported beside the answer, so a silently shortened axis would make that
    report untrue.
    """
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    values = tuple(f"solvent{index}" for index in range(_SWEEP_BUDGET + 1))
    params, refusal = ground_job_params(
        {"smiles": (True, None), "solvents": (True, None)},
        {"smiles": ["a"]},
        {"a": "CCO"},
        Sweep("solvents", values),
        _SWEEP_BUDGET,
    )
    assert params is None
    assert refusal is not None
    assert refusal.code == "axis-too-wide"
    assert str(len(values)) in refusal.detail


def test_a_sweep_exactly_at_the_budget_is_allowed() -> None:
    """The bound is inclusive, so the budget is a number a check can actually spend."""
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    values = tuple(f"solvent{index}" for index in range(_SWEEP_BUDGET))
    params, refusal = ground_job_params(
        {"smiles": (True, None), "solvents": (True, None)},
        {"smiles": ["a"]},
        {"a": "CCO"},
        Sweep("solvents", values),
        _SWEEP_BUDGET,
    )
    assert refusal is None
    assert params is not None
    assert params["solvents"] == list(values)


def test_a_job_naming_no_subject_refuses() -> None:
    """A job naming no subject refuses.

    A job with no required field (e.g. `republish_calculations`) is vacuously "grounded" and would
    be a different kind of act, not a check about a molecule.
    """
    from chemclaw.hypotheses.dispatch import ground_job_params

    params, refusal = ground_job_params({"limit": (False, None)}, {}, {}, None, _SWEEP_BUDGET)
    assert params is None
    assert refusal is not None
    assert refusal.code == "no-subject"


def test_every_job_any_enabled_connector_declares_is_either_pinned_or_refused() -> None:
    """Every job any enabled connector declares is either pinned or refused.

    `find_job` searches every enabled connector, so the repo-wide dispatchable set must equal the
    pinned one.
    """
    from chemclaw.connectors.registry import enabled
    from chemclaw.hypotheses.dispatch import STRUCTURE_FIELDS, SWEEPABLE_FIELDS, ground_job_params

    groundable: set[str] = set()
    for manifest in enabled():
        for job in manifest.jobs:
            fields = _fields_of(manifest.name, job)
            required = [name for name, (req, _) in fields.items() if req]
            subjects = {
                name: (["a"] if STRUCTURE_FIELDS[name] == "one" else ["a", "b"])
                for name in required
                if name in STRUCTURE_FIELDS
            }
            axis = next((name for name in required if name in SWEEPABLE_FIELDS), "")
            sweep = Sweep(parameter=axis, values=("thf",)) if axis else None
            _params, refusal = ground_job_params(
                fields, subjects, {"a": "CCO", "b": "CCN"}, sweep, _SWEEP_BUDGET
            )
            if refusal is None:
                groundable.add(job.name)
    assert groundable == _DISPATCHABLE_JOBS, (
        "a durable job outside the `calc` bundle became launchable by a hypothesis check. "
        "`find_job` searches every enabled connector, so this set — not the per-bundle one — is "
        "what a tournament can actually start."
    )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"subjects": {"reactants": ["a"]}, "sweep": None},
            "subject-field-not-declared",
        ),
        (
            {"subjects": {"smiles": ["a"]}, "sweep": Sweep("solvents", ("thf",))},
            "axis-not-declared",
        ),
    ],
)
def test_a_key_the_job_does_not_declare_refuses_rather_than_being_dropped(
    kwargs: dict[str, Any], code: str
) -> None:
    """A key the job does not declare refuses rather than being dropped.

    These params models do not forbid extras, so pydantic would silently drop the key while the
    report still claimed it.
    """
    from chemclaw.hypotheses.dispatch import ground_job_params

    params, refusal = ground_job_params(
        {"smiles": (True, None), "effort": (False, "quick")},
        kwargs["subjects"],
        {"a": "CCO"},
        kwargs["sweep"],
        _SWEEP_BUDGET,
    )
    assert params is None
    assert refusal is not None
    assert refusal.code == code


#: Every shipped template that requires `smiles` and nothing else, computes, and holds no write
#: tool. Pinned, so a template gaining a second required input or a write step drops out visibly.
_DISPATCHABLE_TEMPLATES = {
    "bond-strength-survey",
    "conformer-refinement",
    "ensemble-free-energy",
    "microspecies-profile",
    "regioselectivity-in-conformer",
    "stereoisomer-ranking",
    "substitution-series",
    "tautomer-resolution",
}


def test_the_dispatchable_template_set_is_exactly_what_is_pinned() -> None:
    """The dispatchable template set is exactly what is pinned, derived from the shipped catalogue.

    Templates chaining an enumerator into a calculation answer questions about structures nobody
    wrote down. Excluded: templates that compute nothing, that act, or that a deployment turned off.
    """
    from chemclaw.agent.authz import STATE_CHANGING_TOOLS
    from chemclaw.hypotheses.dispatch import ground_template_inputs
    from chemclaw.templates.registry import enabled as enabled_templates

    found = set()
    for template in enabled_templates():
        declared = {item.name: item.required for item in template.inputs}
        writes = any(
            getattr(step, "write_tools", None)
            or getattr(step, "tool", None) in STATE_CHANGING_TOOLS
            for step in template.steps
        )
        computes = any(getattr(step, "kind", "") == "job" for step in template.steps)
        _inputs, refusal = ground_template_inputs(declared, "CCO", writes, computes)
        if refusal is None:
            found.add(template.name)
    assert found == _DISPATCHABLE_TEMPLATES, (
        "the set of templates a hypothesis check may run has changed. One that gained a second "
        "required input correctly drops out — nothing in the record supplies it. One that appeared "
        "is runnable unattended by a tournament: read its steps before adding it here."
    )


def test_a_template_requiring_more_than_a_structure_refuses() -> None:
    """The job half's rule on the template half: a second required input would be invented."""
    from chemclaw.hypotheses.dispatch import ground_template_inputs

    inputs, refusal = ground_template_inputs(
        {"smiles": True, "target_ph": True}, "CCO", False, True
    )
    assert inputs is None
    assert refusal is not None
    assert refusal.code == "template-needs-more-than-a-structure"
    assert "target_ph" in refusal.detail


def test_a_template_whose_step_can_act_refuses() -> None:
    """A template whose step can act refuses.

    A template is pre-approved for a person to run, not for a tournament to start unattended.
    """
    from chemclaw.hypotheses.dispatch import ground_template_inputs

    inputs, refusal = ground_template_inputs({"smiles": True}, "CCO", True, True)
    assert inputs is None
    assert refusal is not None
    assert refusal.code == "template-writes"


def test_only_the_structure_is_supplied_and_the_rest_is_disclosed() -> None:
    """Only the structure is supplied, and the unset inputs are disclosed.

    The template's own measured defaults apply (e.g. `tautomer-resolution` pins `level: thorough`).
    The unset inputs are named in the outcome, since a gas-phase answer is a different question from
    the same calculation in water.
    """
    from chemclaw.hypotheses.dispatch import defaulted_inputs, ground_template_inputs

    declared = {"smiles": True, "solvent": False}
    inputs, refusal = ground_template_inputs(declared, "CCO", False, True)
    assert refusal is None
    assert inputs == {"smiles": "CCO"}
    assert defaulted_inputs(declared) == ("solvent",)


def test_a_call_naming_two_targets_is_visible_to_the_dispatcher() -> None:
    """A call naming two targets is refused by the dispatcher, after validation.

    A `model_validator` raising would make `derive_check` fail with non-retryable bad data, leaving
    the hypothesis with no outcome or reason; the JSON schema cannot express mutual exclusion.
    """
    from chemclaw.hypotheses.models import CheckCall

    call = CheckCall(
        tool="predict_pka", template="tautomer-resolution", subject_note_id="compound-x"
    )
    assert call.named_targets == ["predict_pka", "tautomer-resolution"]
    assert CheckCall(tool="predict_pka").named_targets == ["predict_pka"]
    assert CheckCall().named_targets == []


def test_a_template_that_runs_no_calculation_refuses() -> None:
    """A template that runs no calculation refuses.

    A lookup or report would hand the verdict stage prose to read as a computed observation.
    """
    from chemclaw.hypotheses.dispatch import ground_template_inputs

    inputs, refusal = ground_template_inputs({"smiles": True}, "CCO", False, False)
    assert inputs is None
    assert refusal is not None
    assert refusal.code == "template-computes-nothing"


def test_a_job_line_links_only_grounded_structures() -> None:
    """`summarise` keeps the `ran` line's links, so `_job_line` is where model text is unlinked.

    A grounded subject becomes `[[note-id]]`; a swept axis value is the model's choice, and a
    `[[id]]` inside it would otherwise mint an edge on the committed field note.
    """
    from chemclaw.durable.hypothesis_tournament import _job_line
    from chemclaw.hypotheses.dispatch import SWEEPABLE_FIELDS

    axis = sorted(SWEEPABLE_FIELDS)[0]
    line = _job_line(
        "job",
        {"smiles": "CCO", axis: ["water", "[[forged-note]]"]},
        {"smiles": ["compound-x"]},
        {"compound-x": "CCO"},
        {},
    )
    assert "[[compound-x]]" in line
    assert "[[forged-note]]" not in line
    assert "forged-note" in line
