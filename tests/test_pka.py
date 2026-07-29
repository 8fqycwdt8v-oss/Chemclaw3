"""Behavioral tests for the xTB-based pKa predictor (plan step 1c.4, bases in X11).

Runs real GFN2-xTB solvated calculations. Asserts the chemistry ordering (a
carboxylic acid is more acidic than a phenol) and the store integration, rather
than exact values (a semiempirical estimate with ~1.6 pKa uncertainty).

The base half is tested to a different standard, because it *is* a different standard:
aromatic and aryl nitrogen is calibrated to Spearman 1.000 and RMSE ~0.2, while aliphatic
amines are refused outright at Spearman -0.17. Both halves of that split are asserted --
the accuracy and the refusal -- because the refusal is the load-bearing one. A future
change that starts returning a number for trimethylamine has broken something real.
"""

import asyncio
from importlib.metadata import version

import pytest

from chemclaw.core.config import settings
from chemclaw.science.calc.pka import PkaInput, calc_version, predict_pka, run_cached_pka
from chemclaw.science.calc.store import InMemoryStore


def test_calc_version_embeds_engine_build() -> None:
    """The pKa cache key carries the tblite and RDKit builds (D-011).

    An engine or geometry-stack upgrade recomputes rather than serving a stale pKa.
    """
    assert version("tblite") in calc_version()
    assert version("rdkit") in calc_version()


def test_charged_input_raises() -> None:
    """A net-charged acid is rejected: the v1 calibration covers neutral acids only (G4).

    Protonated nicotinic acid (net +1, true pKa ~2) would otherwise run the acid
    at charge 0 and the conjugate base at -1 — both wrong electron counts — and
    return a silently inverted pKa.
    """
    with pytest.raises(ValueError, match="neutral"):
        predict_pka(PkaInput(smiles="OC(=O)c1cccc[nH+]1"))


def test_pka_is_independent_of_smiles_spelling() -> None:
    """Equivalent spellings predict the same pKa (D-011 determinism).

    The cache key canonicalizes, so the computation must run on the canonical
    form too — before the fix, `CCS` vs `SCC` differed by ~2e-3 pKa units.
    Fresh stores force both spellings to actually compute. tblite's SCF carries
    ~1e-12 run-to-run numerical noise, so assert agreement far below chemical
    significance rather than bitwise equality.
    """

    async def _run() -> None:
        first, _ = await run_cached_pka(InMemoryStore(), PkaInput(smiles="CCS"))
        second, _ = await run_cached_pka(InMemoryStore(), PkaInput(smiles="SCC"))
        assert first.pka == pytest.approx(second.pka, abs=1e-8)
        assert first.smiles == second.smiles  # both report the canonical form

    asyncio.run(_run())


def test_acid_ordering_is_physical() -> None:
    """Acetic acid is predicted more acidic (lower pKa) than phenol."""
    acetic = predict_pka(PkaInput(smiles="CC(=O)O"))
    phenol = predict_pka(PkaInput(smiles="Oc1ccccc1"))
    assert acetic.pka < phenol.pka
    # Both land in a sane window for a semiempirical estimate.
    assert 1.0 < acetic.pka < 9.0
    assert 6.0 < phenol.pka < 14.0
    assert acetic.uncertainty > 0


def test_no_acidic_site_raises() -> None:
    """A molecule with no O-H/S-H proton has nothing to deprotonate (gate G4)."""
    with pytest.raises(ValueError, match="no acidic"):
        predict_pka(PkaInput(smiles="c1ccccc1"))


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        predict_pka(PkaInput(smiles="?!not-a-mol"))


def test_cached_pka_computes_once() -> None:
    """A repeat request is served from the store, not recomputed."""

    async def _run() -> None:
        store = InMemoryStore()
        job = PkaInput(smiles="CC(=O)O")
        first, cached1 = await run_cached_pka(store, job)
        second, cached2 = await run_cached_pka(store, job)
        assert cached1 is False
        assert cached2 is True
        assert first.pka == second.pka

    asyncio.run(_run())


# Experimental aqueous pKa values (CRC Handbook / standard references), spanning the
# strongly acidic to the barely acidic end of the v1 domain (carboxylic acids, phenols,
# thiols, alcohols). Held here rather than in a fixture file: twelve literals are easier
# to audit against their source than an opaque data file.
_EXPERIMENTAL_PKA = [
    ("OC(=O)C(F)(F)F", 0.23),
    ("OC=O", 3.75),
    ("OC(=O)CCC(=O)O", 4.21),
    ("OC(=O)c1ccccc1", 4.20),
    ("CC(=O)O", 4.76),
    ("Sc1ccccc1", 6.62),
    ("Oc1ccc([N+](=O)[O-])cc1", 7.15),
    ("Oc1ccccc1", 9.99),
    ("Cc1ccc(O)cc1", 10.26),
    ("CCS", 10.61),
    ("CO", 15.50),
    ("CCO", 15.90),
]


def _spearman(left: list[float], right: list[float]) -> float:
    """Rank correlation of two equal-length samples, with no ties in either.

    Written out rather than pulled from scipy: the inputs are distinct experimental
    values and distinct predictions, so the tie-free formula is exact, and a rank
    correlation is not worth a new dependency.
    """
    ranks = [{value: rank for rank, value in enumerate(sorted(sample))} for sample in (left, right)]
    differences = [ranks[0][a] - ranks[1][b] for a, b in zip(left, right, strict=True)]
    n = len(left)
    return 1 - 6 * sum(d * d for d in differences) / (n * (n * n - 1))


def test_predicted_pka_ranks_acids_correctly() -> None:
    """Across four acid classes the prediction reproduces the experimental *ordering*.

    This is the claim the `ionization-and-partitioning` skill rests on — that the
    predictor is usable for ranking a series and not for setting an absolute pH — so
    it is asserted rather than assumed. The companion bound below is the other half:
    ranking is good, individual values are not good enough for a pH decision.
    """
    experimental = [value for _, value in _EXPERIMENTAL_PKA]
    predicted = [predict_pka(PkaInput(smiles=smiles)).pka for smiles, _ in _EXPERIMENTAL_PKA]
    assert _spearman(experimental, predicted) > 0.9


def test_individual_pka_errors_stay_within_the_reported_uncertainty_on_average() -> None:
    """The reported ~1.6-unit uncertainty is honest: RMSE over the reference set is below it.

    Individual compounds still miss by up to ~2 units (benzoic acid is the worst of
    this set), which is exactly why the skill forbids using a single predicted value
    to choose a process pH — a 2-unit error inverts a "pKa +/- 2" extraction rule.
    """
    errors = [
        predict_pka(PkaInput(smiles=smiles)).pka - value for smiles, value in _EXPERIMENTAL_PKA
    ]
    rmse = (sum(error * error for error in errors) / len(errors)) ** 0.5
    assert rmse < 1.6
    assert max(abs(error) for error in errors) > 1.5  # the reason ranking-only is the rule


# Conjugate-acid pKa (pKaH) of aromatic and aryl nitrogen bases, in water at 25 C
# (CRC / standard tables). These are the seven the base calibration was fitted on, so
# these assertions are in-sample by construction — they guard the *code path*, not the
# method's generalization. The held-out check below is the honest one.
_EXPERIMENTAL_PKAH = [
    ("Nc1ccc([N+](=O)[O-])cc1", 1.00),  # 4-nitroaniline
    ("Nc1ccccc1", 4.60),  # aniline
    ("c1ccncc1", 5.23),  # pyridine
    ("Cc1ccncc1", 6.02),  # 4-methylpyridine
    ("Cc1cccc(C)n1", 6.72),  # 2,6-lutidine
    ("Nc1ccccn1", 6.86),  # 2-aminopyridine
    ("c1cnc[nH]1", 6.95),  # imidazole
]


def test_a_base_is_reported_as_a_base() -> None:
    """Pyridine has no acidic proton, so the base path runs and says so.

    `site` is not decoration: an amine's tabulated number is the pKa of its *conjugate
    acid*, and presenting it as "the pKa" is off by orders of magnitude in the wrong
    direction. The field is what lets the skill say which equilibrium the value describes.
    """
    result = predict_pka(PkaInput(smiles="c1ccncc1"))
    assert result.site == "base"
    assert result.pka == pytest.approx(5.23, abs=1.0)
    # BH+ -> B + H+ is endothermic for any real base; a negative value would mean the
    # protomer search picked something that is not the conjugate acid.
    assert result.deprotonation_energy_kcal > 0


def test_aliphatic_amines_are_refused_rather_than_guessed() -> None:
    """The measurement, enforced: Spearman -0.17 is no ranking ability, so no number (G4).

    Trimethylamine is the compound that exposes it. GFN2 in the gas phase reproduces the
    experimental proton affinity order exactly; ALPB reverses it; and the true aqueous
    order is non-monotonic because aqueous basicity here is set by the ammonium ion's
    hydrogen bonding to water, which a continuum model has no way to represent. No linear
    recalibration recovers a non-monotonic relationship, so this refusal is permanent
    until the solvation treatment changes -- not a threshold waiting to be relaxed.
    """
    for smiles in ("CN(C)C", "C1CCNCC1", "CCN"):  # trimethylamine, piperidine, ethylamine
        with pytest.raises(ValueError, match="aliphatic nitrogen"):
            predict_pka(PkaInput(smiles=smiles))


def test_predicted_pkah_ranks_aromatic_bases_correctly() -> None:
    """The base calibration reproduces the experimental ordering exactly (in sample).

    Spearman 1.000 over the fitted set. Asserted at > 0.95 rather than == 1.0 so a
    geometry-stack upgrade that perturbs one near-tie (2-aminopyridine and imidazole are
    0.09 units apart) reports as the non-event it is instead of a red suite.
    """
    experimental = [value for _, value in _EXPERIMENTAL_PKAH]
    predicted = [predict_pka(PkaInput(smiles=smiles)).pka for smiles, _ in _EXPERIMENTAL_PKAH]
    assert _spearman(experimental, predicted) > 0.95


def test_in_sample_pkah_errors_are_far_below_the_acid_calibrations() -> None:
    """RMSE ~0.2 over pKa 1.0-6.95, against ~1.5 for the acid path.

    Worth pinning because it is counter-intuitive and it is what the skill promises: the
    base half of this predictor is the *accurate* half. Aryl and aromatic nitrogen
    basicity is dominated by delocalization into the ring, which GFN2 with a continuum
    describes well -- the physics the aliphatic case is missing simply is not load-bearing
    here.
    """
    errors = [
        predict_pka(PkaInput(smiles=smiles)).pka - value for smiles, value in _EXPERIMENTAL_PKAH
    ]
    rmse = (sum(error * error for error in errors) / len(errors)) ** 0.5
    assert rmse < 0.5
    assert max(abs(error) for error in errors) < 1.0  # inside the reported uncertainty


def test_held_out_aromatic_bases_land_within_the_reported_uncertainty() -> None:
    """Two bases the calibration never saw, one at each end of its range.

    1,2,3-triazole (pKaH 1.17) and 3,4-lutidine (6.46). This is the only assertion here
    that says anything about generalization, which is why it exists: an in-sample RMSE of
    0.2 over seven points is a fit statistic, not evidence. Both land inside the +/-1.0
    the tool reports.
    """
    for smiles, experimental in (("c1cn[nH]n1", 1.17), ("Cc1ccncc1C", 6.46)):
        result = predict_pka(PkaInput(smiles=smiles))
        assert result.site == "base"
        assert result.pka == pytest.approx(experimental, abs=settings.pka_base_uncertainty)


def test_an_acid_with_a_basic_nitrogen_still_reports_the_acid() -> None:
    """4-aminobenzoic acid: the O-H wins, and the result says `site="acid"`.

    The precedence rule, asserted because reversing it would silently change what "the
    pKa" means for every amphoteric compound. A molecule with a carboxylic acid has a pKa
    in the ordinary sense; that is the number meant by the question. The skill's
    amphoteric section covers the other half -- that the basic centre went unevaluated and
    unmentioned.
    """
    result = predict_pka(PkaInput(smiles="Nc1ccc(C(=O)O)cc1"))
    assert result.site == "acid"
    assert result.uncertainty == settings.pka_uncertainty
    assert 2.0 < result.pka < 8.0  # experimental 4.87


def test_neither_acidic_nor_basic_raises() -> None:
    """Benzene has no proton to lose and no lone pair to gain one on."""
    with pytest.raises(ValueError, match="no acidic .* and no basic nitrogen"):
        predict_pka(PkaInput(smiles="c1ccccc1"))


def test_calc_version_covers_both_calibrations() -> None:
    """Re-tuning either calibration or either uncertainty invalidates the cache (D-011).

    Both live in the one key because one `PkaResult` can come from either path, and a
    stored value carries its uncertainty. Keying only the acid half would serve a base
    result computed under a superseded fit.
    """
    version = calc_version()
    for value in (
        settings.pka_calibration_slope,
        settings.pka_base_calibration_slope,
        settings.pka_base_calibration_intercept,
        settings.pka_uncertainty,
        settings.pka_base_uncertainty,
    ):
        assert str(value) in version
