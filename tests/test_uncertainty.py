"""A prediction says how sure it is, where that came from, and whether to trust it at all.

Three claims are held:

- **"unknown" is not "fine".** `in_domain=None` must never read as trustworthy.
- **The domain check refuses rather than widens.** An out-of-domain prediction gets a flag, not a
  bigger error bar, because extrapolating a linear fit leaves the residuals' distribution.
- **Where the uncertainty came from is part of the claim**, so `method` reaches the rendering.
"""

import ast
from pathlib import Path

import pytest

from chemclaw.connectors.calc.remote import cached_remote
from chemclaw.science.calc.models import SolubilityResult
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.uncertainty import Estimate
from tests.calc_server_fake import FAKE_VERSION, FakeCalcServer, install


def test_an_unknown_domain_is_not_a_trustworthy_one() -> None:
    """`None` means nobody checked, and nobody-checked must not read as fine."""
    unknown = Estimate(value=1.0, unit="log10(mol/L)", uncertainty=0.75, method="reported")
    assert unknown.in_domain is None
    assert unknown.trustworthy is False

    cleared = unknown.model_copy(update={"in_domain": True})
    assert cleared.trustworthy is True


def test_no_structural_domain_check_is_reimplemented_here() -> None:
    """No structural domain check is reimplemented here.

    The screen runs in `Chemclaw3-mcp`'s `servers/calc`, and `SolubilityResult.estimate` arrives
    already carrying the verdict. A local copy with no caller would mislead a reviewer about where
    the control lives and could drift unnoticed.
    """
    import chemclaw.science.calc.uncertainty as module

    assert not hasattr(module, "structural_domain"), (
        "`structural_domain` is back in `science/calc/uncertainty.py`. The live check is "
        "`Chemclaw3-mcp`'s; a second copy here is a claim that a control exists in this "
        "repository, and nothing in `src/` would call it."
    )


async def test_an_out_of_domain_flag_survives_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-domain flag survives the calculation cache.

    The flag travels as a field of a payload this side stores and validates back, so the assertion
    is on the served copy: an old row validating back with `estimate=None` would render as
    "applicability not assessed" for a prediction the calculator refused.
    """
    salt_payload = {
        "calc_version": FAKE_VERSION,
        "smiles": "CCN.Cl",
        "model": "esol-delaney@2004",
        "log_s_mol_per_l": 0.29,
        "uncertainty_log": 0.75,
        "estimate": {
            "value": 0.29,
            "unit": "log10(mol/L)",
            "uncertainty": 0.75,
            "method": "reported",
            "in_domain": False,
            "domain_reasons": ["multi-component (salt or mixture)"],
        },
    }
    server = install(monkeypatch, FakeCalcServer())
    server.overrides["predict_solubility"] = lambda _arguments: salt_payload

    store = InMemoryStore()
    fresh, _ = await cached_remote(store, "predict_solubility", {"smiles": "CCN.Cl"})
    served, cached = await cached_remote(store, "predict_solubility", {"smiles": "CCN.Cl"})
    assert cached is True
    first = SolubilityResult.model_validate(fresh)
    second = SolubilityResult.model_validate(served)
    assert second.estimate is not None
    assert second.estimate.trustworthy is False
    assert second.estimate.domain_reasons, "an out-of-domain prediction gave no reason"
    assert second.estimate == first.estimate


def test_the_trust_rides_on_the_value_line_because_the_excerpt_truncates() -> None:
    """The trust statement rides on the value line, because the excerpt truncates.

    `retrievers._excerpt` is a blind character prefix of the note body, so a trust statement on a
    later line would be cut from exactly the longest notes; a newline would reintroduce that.
    """
    rendered = Estimate(value=-154.75, unit="Hartree", method="none", in_domain=True).render(
        fmt=".6f"
    )
    assert "\n" not in rendered
    assert rendered == "-154.750000 Hartree (no uncertainty established)"


def test_an_unassessed_domain_says_so_rather_than_reading_as_a_pass() -> None:
    """An unassessed domain says so rather than reading as a pass.

    In-domain renders no remark, which is safe only because the other two states are spelled out.
    """
    unknown = Estimate(value=1.0, unit="log10(mol/L)", uncertainty=0.75, method="reported")
    assert "applicability not assessed" in unknown.render()

    passed = unknown.model_copy(update={"in_domain": True})
    assert "applicability not assessed" not in passed.render()
    assert "OUT OF DOMAIN" not in passed.render()


def test_an_out_of_domain_estimate_shouts_and_gives_its_reasons() -> None:
    """The loudest state, reasons included: a flag with no reason is not actionable."""
    rendered = Estimate(
        value=0.5,
        unit="log10(mol/L)",
        uncertainty=0.75,
        method="reported",
        in_domain=False,
        domain_reasons=("net formal charge -1", "non-organic element Fe"),
    ).render(fmt=".3g")
    assert "OUT OF DOMAIN" in rendered
    assert "net formal charge -1" in rendered
    assert "non-organic element Fe" in rendered


def test_the_rendering_distinguishes_where_the_uncertainty_came_from() -> None:
    """A constant from a paper and a figure carried through arithmetic are different claims.

    The ± is identical in both, so if the rendering collapsed `method` the note would state the two
    in the same words — which is the distinction the field was added to preserve (D-2026-08-01).
    """
    base = Estimate(value=1.0, unit="log10(mol/L)", uncertainty=0.5, in_domain=True)
    reported = base.model_copy(update={"method": "reported"}).render(fmt=".3g")
    propagated = base.model_copy(update={"method": "propagated"}).render(fmt=".3g")
    assert reported != propagated
    assert "reported" in reported
    assert "propagated" in propagated
    # Both still carry the number itself, so the difference is in the claim, not the value.
    assert "1 ± 0.5 log10(mol/L)" in reported
    assert "1 ± 0.5 log10(mol/L)" in propagated


def test_a_missing_uncertainty_renders_no_plus_minus_at_all() -> None:
    """`None` must not become `± 0`, which would claim the prediction is exact."""
    rendered = Estimate(value=-154.75, unit="Hartree", in_domain=True).render(fmt=".2f")
    assert "±" not in rendered
    assert "-154.75 Hartree" in rendered


def _identifiers_used_in(path: Path) -> set[str]:
    """Every name a module actually uses: imports, reads, attribute accesses.

    Parsed rather than grepped, so a comment naming a deleted function does not count as a use.
    """
    tree = ast.parse(path.read_text("utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.rsplit(".", 1)[-1])
    return used


def test_no_conformal_interval_is_re_added_without_a_reader() -> None:
    """A split-conformal interval may come back only wired; an unwired re-add fails here.

    The interval and its settings were removed for having no caller and no reader, and the ADR names
    what would justify re-adding them. This forbids the half re-add: a function nothing calls or a
    knob nothing reads, either of which claims a control that does not exist.
    """
    import chemclaw.science.calc.uncertainty as module
    from chemclaw.core.config import settings

    source_root = Path(module.__file__).resolve().parents[3]
    sources = [p for p in source_root.rglob("*.py") if p.name != "uncertainty.py"]

    if hasattr(module, "conformal_uncertainty"):
        callers = [p for p in sources if "conformal_uncertainty" in _identifiers_used_in(p)]
        assert callers, (
            "`conformal_uncertainty` is back in `science/calc/uncertainty.py` with no caller in "
            "`src/`. It was deleted for exactly that once already. Wire it to a predictor, or "
            "leave it out — and see "
            "D-2026-08-27-an-interval-is-only-honest-where-it-was-calibrated for the sample count "
            "that would justify one."
        )

    for knob in type(settings).model_fields:
        if not knob.startswith("calibration_conformal"):
            continue
        # A config module declaring the setting is not a reader of it; that was the whole defect.
        readers = [
            p
            for p in sources
            if "core/config/" not in p.as_posix() and knob in _identifiers_used_in(p)
        ]
        assert readers, (
            f"`{knob}` is a shipped setting with no reader outside `core/config/`, so an operator "
            "can set it and see nothing change — configuration in appearance only. It was deleted "
            "for that once already."
        )
