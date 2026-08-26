"""The ALPB solvent set, re-derived rather than trusted, and the launch-time refusal built on it.

The whole value of `chemclaw.science.calc.solvents` is that its constant is *true of the installed
tblite*. A hand-maintained list that has drifted is worse than no list: it refuses a solvent the
method supports, and there is no error to trace back to it. So the first test here does not read the
constant and nod — it asks tblite about every name in it, and asks tblite about a set of names that
must not be in it.

The rest is the behaviour the constant exists for: `require_supported_solvents` refusing a durable
calc job at launch, in both the shapes a calc job carries a solvent.
"""

import pytest

from chemclaw.science.calc.solvents import (
    ALPB_SOLVENTS,
    SUGGESTED_SOLVENTS,
    is_supported,
    require_supported_solvents,
    unsupported,
)


class _Screen:
    """The `SolventScreenJobSpec` shape: many solvents."""

    def __init__(self, solvents: list[str]) -> None:
        self.solvents = solvents


class _Single:
    """The reaction/scan/ensemble/complex shape: one optional solvent."""

    def __init__(self, solvent: str | None) -> None:
        self.solvent = solvent


def _tblite_accepts(name: str) -> bool:
    """Whether the installed tblite will actually run GFN2-xTB with this ALPB solvent."""
    import numpy as np
    from tblite.interface import Calculator

    calculator = Calculator(
        "GFN2-xTB",
        np.array([8, 1, 1]),
        np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.8], [1.8, 0.0, 0.0]]),
    )
    try:
        calculator.add("alpb-solvation", name)
    except RuntimeError:
        return False
    return True


def test_every_name_in_the_set_is_one_tblite_accepts() -> None:
    """The constant may not advertise a solvent the installed method cannot run.

    This is the direction that produces a *wrong answer* rather than a wrong refusal: a name that
    passes the precondition and then dies inside the durable job is precisely the failure the
    precondition was added to remove, and it would be back with the check apparently in place.
    """
    rejected = sorted(name for name in ALPB_SOLVENTS if not _tblite_accepts(name))
    assert not rejected, (
        f"ALPB_SOLVENTS lists {rejected}, which the installed tblite refuses — re-derive the set "
        "(the module docstring says how) rather than deleting the offending names by hand"
    )


def test_no_solvent_tblite_accepts_is_missing_from_the_set() -> None:
    """The constant may not omit a solvent the method supports, which is the wrong-refusal half.

    Probed against the *dielectric* database compiled into tblite rather than a list written here,
    so this fails when an upgrade adds Born parameters for a solvent — which is the event that would
    otherwise leave the system refusing a perfectly good calculation with no way to notice.
    """
    import re
    from pathlib import Path

    import tblite

    library = next(Path(tblite.__file__).parent.glob("_libtblite*.so"), None)
    if library is None:  # pragma: no cover - a wheel layout we have not met
        pytest.skip("tblite's shared library is not where this probe expects it")
    blob = library.read_bytes()
    # The solvent-name table is a run of NUL-separated lowercase identifiers. Over-collect
    # deliberately: every candidate is then put to tblite itself, so a false positive here costs a
    # probe and a false negative is impossible.
    candidates = {
        token.decode()
        for token in re.findall(rb"[a-z0-9][a-z0-9 ,\-]{2,30}", blob)
        if b"," not in token
    }
    missing = sorted(
        name
        for name in candidates - set(ALPB_SOLVENTS)
        if " " not in name and _tblite_accepts(name)
    )
    assert not missing, (
        f"tblite accepts {missing} for ALPB solvation but ALPB_SOLVENTS omits them, so a chemist "
        "asking for one is refused a calculation the method can do"
    )


def test_the_suggested_shortlist_only_names_supported_solvents() -> None:
    """The list a refusal quotes must be a subset of the set — it used to be neither.

    `xtb_engine.COMMON_SOLVENTS` was a hand-written tuple that omitted `dmf`, `dioxane`, `benzene`
    and `nitromethane` while calling itself "the solvents process chemistry actually asks about".
    Being a strict subset of a measured set is what makes that class of drift impossible.
    """
    assert set(SUGGESTED_SOLVENTS) <= ALPB_SOLVENTS
    assert len(set(SUGGESTED_SOLVENTS)) == len(SUGGESTED_SOLVENTS), "a duplicate in the shortlist"


def test_a_name_is_matched_case_insensitively_and_trimmed() -> None:
    """Matching is tblite's way, so a stricter check here would refuse a name the method takes."""
    assert is_supported("THF")
    assert is_supported(" Water ")
    assert not is_supported("2-MeTHF")


def test_a_solvent_screen_naming_an_unparameterized_solvent_is_refused_at_launch() -> None:
    """The measured live failure: "2-MeTHF" must not reach a workflow.

    Found 2026-08-04 — the model passed the chemist's name faithfully, the turn reported the job
    running, and an activity died ~30 s later on tblite's own message about an epsilon database.
    """
    with pytest.raises(ValueError, match="no parameters for") as raised:
        require_supported_solvents(_Screen(["water", "thf", "2-methyltetrahydrofuran"]))
    message = str(raised.value)
    assert "2-methyltetrahydrofuran" in message
    assert "tetrahydrofuran" in message, "the closest supported spelling is the actionable part"
    assert "water" not in message.split("Commonly used")[0], "only the bad names are named"


def test_a_single_solvent_field_is_checked_too() -> None:
    """The other four calc jobs carry `solvent`, not `solvents`, and are equally launchable."""
    with pytest.raises(ValueError, match="mtbe"):
        require_supported_solvents(_Single("mtbe"))


def test_the_gas_phase_is_not_a_solvent_and_passes() -> None:
    """`solvent: null` is how a calc job asks for gas phase; refusing it would break every one."""
    require_supported_solvents(_Single(None))
    require_supported_solvents(_Screen([]))


def test_a_supported_screen_raises_nothing() -> None:
    """The check must be invisible on every call that was already correct."""
    require_supported_solvents(_Screen(["water", "DMSO", "ethylacetate"]))


def test_an_unknown_name_with_no_close_match_is_refused_without_a_guess() -> None:
    """Proposing `phenol` for a name nothing resembles would be worse than proposing nothing."""
    with pytest.raises(ValueError) as raised:
        require_supported_solvents(_Single("xyzzy"))
    assert "did you mean" not in str(raised.value)


def test_one_bad_name_repeated_is_reported_once() -> None:
    """A screen that names the same typo twice is one mistake, not two."""
    with pytest.raises(ValueError) as raised:
        require_supported_solvents(_Screen(["mtbe", "MTBE", "mtbe "]))
    assert str(raised.value).count("mtbe") == 1


def test_the_bad_names_keep_the_order_they_were_given_in() -> None:
    """So a chemist can line the refusal up against the list they sent, rather than an alphabet."""
    assert unsupported(["mtbe", "water", "cyclohexane", "thf"]) == ["mtbe", "cyclohexane"]


def test_every_declared_job_that_takes_a_solvent_declares_the_precondition() -> None:
    """The rule is only worth anything on the jobs that can violate it — all eleven of them.

    Derived from the manifests and their params models rather than a written-down list, so a new
    solvent-taking job — in this bundle or a new one — fails here rather than in a live run. It
    sweeps every *discovered* bundle, not just the enabled ones, because enablement is a
    deployment's choice and the guard is not.

    The count is pinned deliberately and updating it is the point: the four multi-step jobs added by
    `D-2026-08-25-the-loop-is-a-composite-not-a-template` each take a solvent, and so do
    `profile_rotation` (`D-2026-08-26-a-torsion-is-named-not-indexed`) and
    `rank_species_across_solvents` (`D-2026-08-26-a-solvent-is-an-argument-not-a-job`), the latter
    taking `solvents`, plural. Every one of them came through this assertion to get here — two of
    them on branches that did not know about each other, which is exactly when a sweep that adapted
    silently would let the next one arrive with no precondition and fail thirty seconds into a
    durable run with tblite's own "String value for epsilon was not found among database of
    solvents".
    """
    from chemclaw.connectors.jobs import _params_model
    from chemclaw.connectors.registry import discovered

    checked = 0
    for name, (_, manifest) in discovered().items():
        for job in manifest.jobs:
            if not set(_params_model(name, job).model_fields) & {"solvent", "solvents"}:
                continue
            checked += 1
            assert (
                job.precondition == "chemclaw.science.calc.solvents:require_supported_solvents"
            ), f"job {job.name!r} takes a solvent but declares precondition {job.precondition!r}"
    assert checked == 12, f"expected the twelve solvent-taking calc jobs, swept {checked}"


def test_the_launcher_refuses_the_screen_before_it_starts_any_durable_work() -> None:
    """End to end through the real seam — a declaration nothing calls would guard nothing.

    `prepare_job_launch` is the single place both launchers (the generated agent tool and the
    template workflow's job step, D-168) validate, authorize and run the precondition, and it is
    reached before any workflow is started. Driving it here is what proves the manifest line above
    is wired rather than merely present.
    """
    from chemclaw.connectors.jobs import prepare_job_launch
    from chemclaw.connectors.registry import discovered

    (_, manifest) = discovered()["calc"]
    (job,) = [spec for spec in manifest.jobs if spec.name == "compare_solvents"]
    params = {
        "reactants": ["CC(=O)O"],
        "products": ["CC(=O)[O-]"],
        "solvents": ["water", "2-methyltetrahydrofuran"],
    }
    with pytest.raises(ValueError, match="2-methyltetrahydrofuran"):
        prepare_job_launch("calc", job, params)
    params["solvents"] = ["water", "thf"]
    assert prepare_job_launch("calc", job, params)["solvents"] == ["water", "thf"]
