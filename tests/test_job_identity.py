"""A durable job's idempotency key covers the versioned inputs, not only the payload (DARK-4).

The defect: `job_workflow_id` hashed `[connector, job, payload]`. Change `xtb_method` and the
calculation store correctly misses and recomputes — while `start_workflow` raised
`WorkflowAlreadyStartedError`, rejoined the **completed** prior run, and returned numbers the old
method produced. `science/calc/store.py` has always taken the opposite and correct position for the
same computations, with `calc_version` in its key.

The two layers do different jobs, and that is what makes the fix cheap: the workflow id dedups
*launches*, the calculation store dedups *computation*. A changed setting re-executes the workflow,
whose every activity then hits the cache and returns the same values immediately unless the
calculator's own version changed too (D-011).
"""

import pytest

from chemclaw.connectors.jobs import identity_settings, job_workflow_id
from chemclaw.core.config import settings

_PAYLOAD = {"reactants": ["CC(=O)O"], "products": ["CC(=O)[O-]", "[H+]"]}


def test_changing_the_method_changes_the_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding, in the shape the review found it.

    `xtb_method` is deployment configuration rather than a launch argument, so it never reached the
    payload — and the id is what Temporal dedups on.
    """
    before = job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD)
    monkeypatch.setattr(settings, "xtb_method", "GFN1-xTB")
    after = job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD)

    assert before != after, (
        "a re-launch after a method change resolves to the completed prior run, which returns "
        "numbers the old method produced"
    )


def test_a_calibration_constant_changes_it_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row names the method and a calibration constant, and both are `pka_*`/`xtb_*` settings.

    Prefixes rather than a hand-written list of settings, so a knob added tomorrow is covered the
    day it is added rather than the day someone remembers the declaration.
    """
    before = job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD)
    monkeypatch.setattr(settings, "pka_calibration_slope", 0.3)
    assert job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD) != before


def test_an_unchanged_deployment_still_rejoins_its_own_run() -> None:
    """The property the key exists for: a duplicate launch must resolve to the same id.

    Without this the fix would trade a wrong answer for an unbounded bill — every repeat launch a
    fresh run.
    """
    assert job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD) == job_workflow_id(
        "calc", "compute_reaction_energy", _PAYLOAD
    )


def test_a_setting_outside_the_declared_prefixes_does_not_change_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound on the blast radius.

    If every setting entered the key, every deploy-time config edit would re-run every job — which
    is `deployment_revision` in the key, the option this rejected.
    """
    before = job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD)
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 999.0)
    assert job_workflow_id("calc", "compute_reaction_energy", _PAYLOAD) == before


def test_a_bundle_declaring_nothing_keeps_its_previous_id() -> None:
    """No in-flight workflow is orphaned by this, and no fixture bundle in the suite is affected.

    The id for a bundle with no `identity_settings` must be byte-identical to the pre-DARK-4 form,
    which is `stable_hash([connector, job, payload])` — asserted against that expression rather
    than against a recorded literal, so it stays true if the hash function is ever revised.
    """
    from chemclaw.core.ids import stable_hash

    payload = {"subject": "benzene"}
    assert identity_settings("fixture") == {}
    assert job_workflow_id("fixture", "run_fixture_job", payload) == (
        f"fixture-run_fixture_job-{stable_hash(['fixture', 'run_fixture_job', payload])}"
    )


def test_the_declared_prefixes_actually_resolve_to_settings() -> None:
    """A prefix matching nothing reads as a declaration and contributes nothing to the id.

    That is the exact defect `identity_settings` closes, coming back with the declaration
    apparently in place — so `make connector-validate` refuses it, and this pins that the shipped
    bundle passes rather than merely declares.
    """
    resolved = identity_settings("calc")
    assert "xtb_method" in resolved
    assert "pka_calibration_slope" in resolved
    assert resolved["xtb_method"] == settings.xtb_method


def test_the_validator_refuses_a_prefix_that_matches_no_setting() -> None:
    """The guard on the declaration itself, driven through the real check."""
    from chemclaw.cli.validate_connectors import _identity_settings_problems
    from chemclaw.connectors.manifest import ConnectorManifest

    manifest = ConnectorManifest.model_validate(
        {
            "name": "probe",
            "description": "a probe bundle",
            "jobs": [{"name": "probe_job", "workflow": "W", "summary": "s"}],
            "identity_settings": ["xtb_", "no_such_setting_"],
        }
    )
    (problem,) = _identity_settings_problems(manifest)
    assert "no_such_setting_" in problem


def test_an_unknown_connector_yields_no_identity_rather_than_raising() -> None:
    """Operator scripts and tests call this with fixture names, and a lookup miss is not a failure.

    A raise here would turn "this bundle is not installed" into a launch error, on the one path
    whose whole job is to produce a deterministic string.
    """
    assert identity_settings("no-such-bundle") == {}
