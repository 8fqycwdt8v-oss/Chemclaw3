"""The ALPB solvent set, re-derived from the installed tblite, and the launch-time refusal on it.

Every name in the constant is checked against tblite, and so is a set of names that must not be
in it. `require_supported_solvents` refuses a durable calc job at launch in both of a job's
solvent shapes.
"""

import pytest

from chemclaw.science.calc.solvents import (
    ALPB_SOLVENTS,
    SUGGESTED_SOLVENTS,
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

    Such a name would pass the precondition and fail inside the durable job.
    """
    rejected = sorted(name for name in ALPB_SOLVENTS if not _tblite_accepts(name))
    assert not rejected, (
        f"ALPB_SOLVENTS lists {rejected}, which the installed tblite refuses — re-derive the set "
        "(the module docstring says how) rather than deleting the offending names by hand"
    )


def test_no_solvent_tblite_accepts_is_missing_from_the_set() -> None:
    """The constant may not omit a solvent the method supports.

    Probed against tblite's compiled dielectric database, so an upgrade adding a solvent fails here.
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
    """The shortlist a refusal quotes is a subset of the supported set."""
    assert set(SUGGESTED_SOLVENTS) <= ALPB_SOLVENTS
    assert len(set(SUGGESTED_SOLVENTS)) == len(SUGGESTED_SOLVENTS), "a duplicate in the shortlist"


def test_a_name_is_matched_case_insensitively_and_trimmed() -> None:
    """Matching is case-insensitive and trimmed, as tblite's is, asserted through `unsupported`."""
    assert unsupported(["THF", " Water "]) == []
    assert unsupported(["2-MeTHF"]) == ["2-MeTHF"]


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
    """Every declared job that takes a solvent declares the precondition.

    Derived from every discovered bundle's manifests and params models, since enablement is a
    deployment choice. The count is pinned deliberately, so a new solvent-taking job must pass
    through this assertion.
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
    """The launcher refuses the screen before starting any durable work.

    `prepare_job_launch` is the single place both launchers run the precondition, so driving it
    proves the manifest line is wired.
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
