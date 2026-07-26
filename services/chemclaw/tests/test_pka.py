"""Behavioral tests for the xTB-based pKa predictor (plan step 1c.4).

Runs real GFN2-xTB solvated calculations. Asserts the chemistry ordering (a
carboxylic acid is more acidic than a phenol) and the store integration, rather
than exact values (a semiempirical estimate with ~1.6 pKa uncertainty).
"""

import asyncio
from importlib.metadata import version

import pytest

from calc.pka import PkaInput, _calc_version, predict_pka, run_cached_pka
from calc.store import InMemoryStore


def test_calc_version_embeds_engine_build() -> None:
    """The pKa cache key carries the tblite and RDKit builds (D-011).

    An engine or geometry-stack upgrade recomputes rather than serving a stale pKa.
    """
    assert version("tblite") in _calc_version()
    assert version("rdkit") in _calc_version()


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
