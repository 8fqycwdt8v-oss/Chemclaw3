"""Refusing to run a discriminating check unless every argument is grounded.

The subject of these tests is a policy, not an algorithm: *nothing a model writes becomes a tool
argument*. So most of them are refusals, and each one names the specific way a check could
otherwise have reached a calculator with a value nobody checked.
"""

import inspect

import pytest

from chemclaw.agent.template_surface import ToolArguments, resolvable_signatures
from chemclaw.hypotheses.dispatch import (
    STRUCTURE_ARGUMENT,
    Dispatch,
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
    """`screen_hazards` takes a *list* of SMILES, and a keys-only check would have passed it.

    That is exactly why this module adds a type to the three terms `ToolArguments` carries: for a
    template the argument's type is unknown until substitution, but a check's subject is a SMILES
    and is known now.
    """
    refusal = refuse_unless_dispatchable(
        "screen_hazards", _contract(required={"smiles"}, kind="array")
    )
    assert refusal is not None
    assert refusal.code == "structure-is-not-a-string"


def test_an_unreadable_contract_refuses_rather_than_passing() -> None:
    """The opposite polarity to `core/connect.signature_mismatch`, and deliberately.

    That function returns "nothing to say" for a callable it cannot introspect because a driver
    refuses a bad keyword at construction. A calculator does not refuse — it computes, and the
    number comes back looking like an answer. Where the fallback is a plausible number,
    permissiveness is the defect.
    """
    refusal = refuse_unless_dispatchable("mystery", ToolContract(readable=False))
    assert refusal is not None
    assert refusal.code == "unreadable-contract"


def test_an_absent_tool_refuses_and_says_so() -> None:
    refusal = refuse_unless_dispatchable("not_here", None)
    assert refusal is not None
    assert refusal.code == "tool-unavailable"


def test_a_check_naming_no_tool_refuses() -> None:
    refusal = refuse_unless_dispatchable("", _contract(required={"smiles"}))
    assert refusal is not None
    assert refusal.code == "no-tool-named"


def test_a_tool_whose_default_is_wrong_for_the_molecule_refuses() -> None:
    """Arity is necessary and not sufficient, which is the finding that cost the most here.

    `compute_thermochemistry` passes every signature test: one required argument, the structure.
    Its `symmetry_number` defaults to 1 — "no symmetry" — which is false for any symmetric molecule
    and moves the entropy, and therefore the free energy, by `R·ln(sigma)` with nothing in the
    result saying so. A purely computed allowlist would have shipped that number.
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


#: Every tool in this tree that a check may be dispatched onto today. Pinned rather than counted:
#: the set is what a deployment's chemists can have computed for them automatically, so it moves by
#: a deliberate edit and a reviewer sees which name moved.
#:
#: **A tool gaining a required argument drops out of this set on its own**, which is the property
#: the whole design rests on and the reason this test exists: the failure lands here, in a diff,
#: rather than in a chemist's result.
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


def _job_fields(job_name: str) -> dict[str, tuple[bool, object]]:
    """One shipped calc job's declared params, reduced to what `ground_job_params` reads."""
    from chemclaw.connectors.jobs import _params_model
    from chemclaw.connectors.registry import discovered

    _bundle, manifest = discovered()["calc"]
    job = next(spec for spec in manifest.jobs if spec.name == job_name)
    model = _params_model("calc", job)
    return {name: (f.is_required(), f.default) for name, f in model.model_fields.items()}


#: Jobs a tournament may launch on its own, derived from each job's declared params model. The
#: three that are absent are absent for one reason, and it is the reason this module exists:
#: `scan_coordinate` and `profile_rotation` need **atom indices**, `survey_bond_strengths` a
#: cleavage list, and a model asked for any of those produces them plausibly and wrongly. They
#: become dispatchable when something *enumerates* them — which is the repo's standing rule that
#: enumeration and calculation are separate tools and the order is not optional.
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


def _try_ground(job_name: str) -> tuple[dict[str, object] | None, object]:
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
    return ground_job_params(fields, subjects, {"a": "CCO", "b": "CCN"}, sweep)


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

    _, refusal = ground_job_params({"mystery": (True, None)}, {}, {}, None)
    assert refusal is not None
    assert refusal.code == "field-cannot-be-grounded"


def test_a_swept_axis_must_have_a_vocabulary_behind_it() -> None:
    """Varying is allowed; varying over values nothing validates is not."""
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None)}, {"smiles": ["a"]}, {"a": "CCO"}, Sweep("temperature_k", ("300",))
    )
    assert refusal is not None
    assert refusal.code == "axis-not-sweepable"


def test_a_sweep_over_no_values_refuses() -> None:
    from chemclaw.hypotheses.dispatch import Sweep, ground_job_params

    _, refusal = ground_job_params({}, {}, {}, Sweep("solvents", ()))
    assert refusal is not None
    assert refusal.code == "axis-empty"


def test_a_subject_that_did_not_resolve_refuses_rather_than_dropping() -> None:
    """Silently shortening a reactant list would change the reaction being computed."""
    from chemclaw.hypotheses.dispatch import ground_job_params

    _, refusal = ground_job_params(
        {"reactants": (True, None)}, {"reactants": ["a", "ghost"]}, {"a": "CCO"}, None
    )
    assert refusal is not None
    assert refusal.code == "subject-not-found"
    assert "ghost" in refusal.detail


def test_a_scalar_structure_field_refuses_several_subjects() -> None:
    from chemclaw.hypotheses.dispatch import ground_job_params

    _, refusal = ground_job_params(
        {"smiles": (True, None)}, {"smiles": ["a", "b"]}, {"a": "CCO", "b": "CCN"}, None
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
    )
    assert refusal is None
    assert params is not None
    assert params["solvents"] == ["thf", "dmf", "toluene"]

    _bundle, manifest = discovered()["calc"]
    job = next(spec for spec in manifest.jobs if spec.name == "compare_solvents")
    # The declared params model is the authority, exactly as `prepare_job_launch` uses it.
    _params_model("calc", job).model_validate(params)
