"""F8-T1: a prediction says how sure it is, where that came from, and whether to trust it at all.

The row: `implementation-tickets.md` specified a `science/calc/uncertainty.py` module with
`value + uncertainty + in_domain + method`; none of it existed, so `predict_solubility` on a
molecule ESOL cannot describe returned a confident number with a training-set RMSE attached and
nothing machine-readable said otherwise.

The tests below hold three claims that are easy to state and easy to get subtly wrong:

- **"unknown" is not "fine".** `in_domain=None` must never read as trustworthy.
- **The domain check refuses rather than widens.** An out-of-domain prediction does not get a bigger
  error bar; it gets a flag, because extrapolating a linear fit leaves the distribution its
  residuals were drawn from.
- **Where the uncertainty came from is part of the claim.** Two numbers rendered identically say
  different things, so `method` has to reach the rendering rather than stopping at the model.
"""

import asyncio

import pytest
from rdkit import Chem

from chemclaw.connectors.calc.remote import cached_remote
from chemclaw.science.calc.models import SolubilityResult
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.uncertainty import Estimate, structural_domain
from tests.calc_server_fake import FAKE_VERSION, FakeCalcServer, install


def test_an_unknown_domain_is_not_a_trustworthy_one() -> None:
    """`None` means nobody checked, and nobody-checked must not read as fine.

    The whole reason `in_domain` is three-valued: a calculator with no declared domain has not
    cleared its prediction, and a consumer that treats a missing answer as an affirmative one is
    the bug the third value exists to expose.
    """
    unknown = Estimate(value=1.0, unit="log10(mol/L)", uncertainty=0.75, method="reported")
    assert unknown.in_domain is None
    assert unknown.trustworthy is False

    cleared = unknown.model_copy(update={"in_domain": True})
    assert cleared.trustworthy is True


def test_a_salt_is_out_of_domain_for_a_single_molecule_model() -> None:
    """Not "less accurate" — undefined. The equation has no term for the counter-ion."""
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CCN.Cl"))
    assert in_domain is False
    assert any("multi-component" in reason for reason in reasons)


def test_a_charged_species_is_out_of_domain() -> None:
    """Crippen contributions are parameterised for neutral species.

    An ionised form's solubility differs from its neutral form's by orders of magnitude, which is
    far outside anything a 0.75-log RMSE describes — so reporting that RMSE beside it is the
    specific misleading answer this check exists to stop.
    """
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CC(=O)[O-]"))
    assert in_domain is False
    assert any("formal charge" in reason for reason in reasons)


def test_an_organometallic_is_out_of_domain() -> None:
    """RDKit returns a logP for a ferrocene-like structure rather than refusing.

    That is the dangerous shape: a number, with no signal that the descriptor sum skipped the part
    of the molecule that matters.
    """
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("[Fe]"))
    assert in_domain is False
    assert any("non-organic" in reason for reason in reasons)


def test_an_ordinary_neutral_organic_is_in_domain() -> None:
    """The check must clear the common case, or it is a flag nobody reads."""
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CCO"))
    assert in_domain is True
    assert reasons == ()


def test_an_out_of_domain_flag_survives_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row's own example, end to end — and the half of it this repository still owns.

    The ESOL prediction itself is the calculation server's since
    `D-2026-08-16-the-physics-leaves-the-cache-stays`, but the *flag reaching a chemist* is not: it
    travels as a field of a payload this side stores and validates back. That is exactly where it
    was lost once before — `SolubilityResult` gained `estimate`, no version moved, and every row
    already on disk validated back with `estimate=None`, which `Estimate.render` spells as
    "applicability not assessed". A salt the calculator refuses to speak about came back looking
    merely unchecked, forever, because `durable/retention.py` never prunes `calculation_results`.

    So the assertion is on the *served* copy, not the fresh one: an out-of-domain estimate that
    does not survive a cache hit is the same defect through a shorter route.
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

    async def _run() -> None:
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

    asyncio.run(_run())


def test_the_trust_rides_on_the_value_line_because_the_excerpt_truncates() -> None:
    """`render` is one fragment, not a stanza, and that is a constraint rather than a preference.

    `chemclaw.retrieval.retrievers._excerpt` is a blind character prefix of the note body. A trust
    statement placed below the value is therefore cut from precisely the notes with the most prose
    and kept in the short ones that needed it least — so the rendering has to be inline, and a
    newline in it would silently reintroduce the separation.
    """
    rendered = Estimate(value=-154.75, unit="Hartree", method="none", in_domain=True).render(
        fmt=".6f"
    )
    assert "\n" not in rendered
    assert rendered == "-154.750000 Hartree (no uncertainty established)"


def test_an_unassessed_domain_says_so_rather_than_reading_as_a_pass() -> None:
    """The rendering must not let silence do the work of an affirmative answer.

    In-domain renders no domain remark — so a bare line means the check ran and passed. That is
    only safe because the other two states are spelled out; if `None` were also silent, a reader
    could not tell an unasked question from an answered one, which is the whole reason `in_domain`
    has three values rather than two.
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
