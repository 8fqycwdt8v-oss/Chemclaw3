"""An unbalanced equation is refused at launch, with the message the model can act on (VIBE-1a).

The finding, from the live full-stack pass: `compute_reaction_energy` launched, `CalcJobWorkflow`
correctly refused the equation, and the tool raised `WorkflowFailureError: Workflow execution
failed` while the actionable text — "reaction is not atom-balanced (reactants minus products): C +2,
H +4, O +2" — stayed in the worker log. Temporal retried the refusal five times on the way.

Balance is knowable before any durable work starts, so it is a `preconditions` entry: the message
reaches the model through `surface_domain_errors` in the same turn, and nothing is retried.

That needed a second slot. `preconditions` was one string, and these jobs need two unrelated rules
over one spec, so the manifest field became a list — the tests for that shape are here too, because
it exists for this.
"""

from typing import Any

import pytest
import yaml

from chemclaw.connectors.jobs import prepare_job_launch
from chemclaw.connectors.manifest import ConnectorManifest, JobSpec
from chemclaw.connectors.registry import discovered
from chemclaw.science.calc.balance import check_balance, require_balanced_equation


def _job(connector: str, name: str) -> JobSpec:
    """The declared job as a deployment loads it, not a hand-built stand-in."""
    (_, manifest) = discovered()[connector]
    return next(job for job in manifest.jobs if job.name == name)


def test_the_launcher_refuses_an_unbalanced_equation_before_any_durable_work() -> None:
    """The finding, driven through the real seam.

    `prepare_job_launch` is where both launchers (the generated agent tool and the template
    workflow's job step, D-168) validate, authorize and run the preconditions, and it runs before
    any workflow is started — so a refusal here is a `ValueError` the model reads rather than a
    `WorkflowFailureError` after five retries.
    """
    unbalanced = {"reactants": ["c1ccccc1", "OO"], "products": ["c1ccccc1O"], "solvent": "water"}
    with pytest.raises(ValueError, match="not atom-balanced"):
        prepare_job_launch("calc", _job("calc", "compute_reaction_energy"), unbalanced)


def test_the_refusal_names_the_imbalance_element_by_element() -> None:
    """What makes it repairable: the model needs to know *what* is missing, not that something is.

    "reaction is not atom-balanced" alone is the same dead end as `Workflow execution failed`, one
    layer up.
    """
    with pytest.raises(ValueError, match=r"H [+-]\d"):
        prepare_job_launch(
            "calc",
            _job("calc", "compute_reaction_energy"),
            {"reactants": ["CC(=O)O"], "products": ["CC(=O)[O-]"]},
        )


def test_a_balanced_equation_still_launches() -> None:
    """The bound: the guard must not cost the capability."""
    payload = prepare_job_launch(
        "calc",
        _job("calc", "compute_reaction_energy"),
        {"reactants": ["CC(=O)O"], "products": ["CC(=O)[O-]", "[H+]"], "solvent": "water"},
    )
    assert payload["products"] == ["CC(=O)[O-]", "[H+]"]


def test_a_charge_imbalance_is_refused_too() -> None:
    """Atoms balancing while charge does not is the quieter half, and equally meaningless."""
    with pytest.raises(ValueError, match="charge-balanced"):
        prepare_job_launch(
            "calc",
            _job("calc", "compute_reaction_energy"),
            {"reactants": ["[Cl-]"], "products": ["[Cl]"]},
        )


def test_the_screen_carries_the_same_rule() -> None:
    """`compare_solvents` runs the same equation in several solvents, so it can be as wrong."""
    with pytest.raises(ValueError, match="not atom-balanced"):
        prepare_job_launch(
            "calc",
            _job("calc", "compare_solvents"),
            {"reactants": ["c1ccccc1", "OO"], "products": ["c1ccccc1O"], "solvents": ["water"]},
        )


def test_a_spec_with_no_equation_passes_untouched() -> None:
    """The rule must be declarable beside another without knowing which jobs the other covers.

    A conformer search carries no equation. If this raised, one rule would have to know the shape
    of every job the *other* rule applies to — which is the coupling a list of independent rules
    exists to avoid.
    """
    require_balanced_equation(object())


def test_every_job_that_carries_an_equation_declares_the_rule() -> None:
    """Derived from the params models, so a third equation-taking job fails here, not in a run.

    The same sweep shape as `test_solvents.py`'s, and for the same reason: a declaration is only
    worth what the jobs that can violate it actually declare.
    """
    from chemclaw.connectors.jobs import _params_model

    checked = 0
    for name, (_, manifest) in discovered().items():
        for job in manifest.jobs:
            fields = set(_params_model(name, job).model_fields)
            if not {"reactants", "products"} <= fields:
                continue
            checked += 1
            assert "chemclaw.science.calc.balance:require_balanced_equation" in job.preconditions, (
                f"job {job.name!r} carries an equation but declares {job.preconditions!r}"
            )
    assert checked == 2, f"expected the two equation-taking calc jobs, swept {checked}"


# --- The manifest field that made two rules possible ------------------------------------------


def _manifest(job_yaml: str) -> ConnectorManifest:
    """Load a one-job manifest from YAML, exactly as the registry does."""
    return ConnectorManifest.model_validate(
        yaml.safe_load(
            "name: probe\ndescription: a probe bundle\njobs:\n"
            + job_yaml
            + "    params:\n      - {name: subject, type: string, description: What.}\n"
        )
    )


def test_a_job_may_declare_several_preconditions_and_they_run_in_order() -> None:
    """Why the field is a list: two unrelated rules over one spec, each stated once.

    One slot forces a combining function per *combination* — a cross-product of rules that each
    want to exist independently. The order is the manifest's, and the first refusal wins, so a job
    puts its cheapest or most-likely rule first.
    """
    manifest = _manifest(
        "  - name: probe_job\n"
        "    workflow: ProbeWorkflow\n"
        "    summary: A probe.\n"
        "    preconditions:\n"
        "      - tests.test_balance_precondition:_refuse_first\n"
        "      - tests.test_balance_precondition:_refuse_second\n"
    )
    with pytest.raises(ValueError, match="first"):
        prepare_job_launch("probe", manifest.jobs[0], {"subject": "x"})


def test_a_later_precondition_still_runs_when_the_earlier_one_passes() -> None:
    """Otherwise "several preconditions" would mean "the first precondition"."""
    manifest = _manifest(
        "  - name: probe_job\n"
        "    workflow: ProbeWorkflow\n"
        "    summary: A probe.\n"
        "    preconditions:\n"
        "      - tests.test_balance_precondition:_allow\n"
        "      - tests.test_balance_precondition:_refuse_second\n"
    )
    with pytest.raises(ValueError, match="second"):
        prepare_job_launch("probe", manifest.jobs[0], {"subject": "x"})


def test_a_precondition_that_is_not_a_reference_is_refused_at_load() -> None:
    """The pattern moved from a `Field(pattern=...)` to a validator, so it is pinned here.

    A list field cannot carry a per-item pattern the way a string field does, and losing the check
    silently would turn a typo into an `ImportError` at the first launch instead of a manifest
    error at load.
    """
    with pytest.raises(ValueError, match="module:function"):
        _manifest(
            "  - name: probe_job\n"
            "    workflow: ProbeWorkflow\n"
            "    summary: A probe.\n"
            "    preconditions:\n"
            "      - not a reference\n"
        )


def test_declaring_no_precondition_is_still_fine() -> None:
    """Most jobs have no domain rule at all; the default must stay 'none', not 'missing'."""
    manifest = _manifest(
        "  - name: probe_job\n    workflow: ProbeWorkflow\n    summary: A probe.\n"
    )
    assert manifest.jobs[0].preconditions == []
    assert prepare_job_launch("probe", manifest.jobs[0], {"subject": "x"})["subject"] == "x"


def _refuse_first(_spec: Any) -> None:
    """Refuse, naming itself so the order is observable."""
    raise ValueError("the first rule refused")


def _refuse_second(_spec: Any) -> None:
    """Refuse, naming itself so the order is observable."""
    raise ValueError("the second rule refused")


def _allow(_spec: Any) -> None:
    """Pass, so the rule after it is reached."""


def test_the_rule_has_one_definition_shared_with_the_workflow() -> None:
    """The launch check and the workflow's own check must not be able to disagree.

    `reaction.py` imports `check_balance` from here rather than keeping its copy — a second
    definition is how "balanced" would come to mean two things, with the launch accepting what the
    workflow then refuses.
    """
    import chemclaw.science.calc.reaction as reaction

    # `getattr`, not an attribute access: mypy's no-implicit-reexport is right that a module is not
    # a re-export surface, and the *point* of this test is that `reaction` holds this exact object
    # rather than one of its own.
    assert getattr(reaction, "check_balance") is check_balance  # noqa: B009
