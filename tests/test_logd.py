"""The local half of logD: a Crippen sum, one Henderson-Hasselbalch term, and its domain.

logD is composed from a cached pKa (calculation server) and RDKit, so these drive `logd_from_pka`
with a supplied `PkaResult`: the arithmetic under test is what runs in production. Asserted: the
direction of the correction (opposite for a base), the domain (one term cannot describe two
ionised sites), and the site enumeration deciding which a molecule is. The pKa values are those
the shipped predictor produced, so the pinned outputs are the whole composition's.
"""

import math

import pytest
from rdkit import Chem
from rdkit.Chem import Crippen

from chemclaw.science.calc.logd import ionisable_sites, logd_from_pka
from chemclaw.science.calc.models import PkaResult
from chemclaw.science.calc.uncertainty import CalculationDomainError

_BENZOIC_ACID = "OC(=O)c1ccccc1"
_SUCCINIC_ACID = "OC(=O)CCC(=O)O"  # diprotic: both carboxyls ionised at pH 7.4
_GLYCINE = "NCC(=O)O"  # amphoteric: a carboxyl and an aliphatic amine
_PARACETAMOL = "CC(=O)Nc1ccc(O)cc1"  # a phenol plus an amide N, which is *not* a base
_PYRIDINE = "c1ccncc1"


def _pka(smiles: str, value: float, site: str = "acid") -> PkaResult:
    """A `PkaResult` as the calculation server returns one, canonicalized the same way."""
    canonical = Chem.CanonSmiles(smiles)
    return PkaResult(
        smiles=canonical,
        method="GFN2-xTB/alpb-water",
        pka=value,
        deprotonation_energy_kcal=320.0,
        uncertainty=1.6 if site == "acid" else 1.0,
        site=site,  # type: ignore[arg-type]
    )


def test_logd_defaults_to_the_configured_ph() -> None:
    """Omitting `ph` uses `settings.logd_default_ph` (7.4), not an arbitrary constant."""
    from chemclaw.core.config import settings

    result = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784))
    assert result.ph == settings.logd_default_ph


def test_logd_increases_as_ph_drops_below_the_pka() -> None:
    """Below the pKa the acid is mostly neutral: logD rises toward logP as pH falls."""
    physiological = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784), ph=7.4)
    acidic = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784), ph=1.0)
    assert acidic.log_d > physiological.log_d
    # Far below the pKa, logD approaches logP (the fully neutral limit).
    assert acidic.log_d == pytest.approx(acidic.clogp, abs=0.05)


def test_the_uncertainty_is_a_propagation_of_its_two_inputs_and_not_a_copy() -> None:
    """LogD's error bar propagates Crippen's RMSE and the pKa's, each through its own derivative.

    `dlogD/dclogP = 1` and `dlogD/dpKa` is the ionised fraction, near zero for the molecules this
    composition serves, so copying the pKa residual is neither a propagation nor usually dominant.
    Pyridine at pH 7.4 is 0.67 % ionised.
    """
    from chemclaw.core.config import settings

    pka_uncertainty = 1.0
    result = logd_from_pka(_pka(_PYRIDINE, 5.23, site="base"), ph=7.4)

    # Derived here, from the Henderson-Hasselbalch expression rather than from the code under test.
    ionised_ratio = 10.0 ** (5.23 - 7.4)
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    crippen = settings.crippen_logp_uncertainty
    expected = math.hypot(crippen, ionised_fraction * pka_uncertainty)
    print(
        f"reported {result.uncertainty!r} against propagated {expected!r}; "
        f"pKa term = {ionised_fraction * pka_uncertainty!r}, Crippen term = {crippen!r}"
    )
    assert ionised_fraction == pytest.approx(0.006715427889235968, abs=1e-12)
    assert result.uncertainty == pytest.approx(expected, abs=1e-12)
    # The claim the old docstring made, now checkable: the pKa is *not* the dominant term here.
    assert ionised_fraction * pka_uncertainty < 0.02 < crippen


def test_a_fully_ionised_acid_carries_both_terms_in_quadrature() -> None:
    """The other end of the same derivative: at f ≈ 1 the pKa term arrives essentially in full.

    Benzoic acid at pH 7.4 is 93 % ionised, so the bar must be larger than either input alone —
    the direction the copied value got wrong the *other* way, understating rather than overstating.
    """
    from chemclaw.core.config import settings

    result = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784), ph=7.4)
    ionised_ratio = 10.0 ** (7.4 - 6.2784)
    ionised_fraction = ionised_ratio / (1.0 + ionised_ratio)
    expected = math.hypot(settings.crippen_logp_uncertainty, ionised_fraction * 1.6)
    print(f"reported {result.uncertainty!r} against propagated {expected!r}")
    assert result.uncertainty == pytest.approx(expected, abs=1e-12)
    assert result.uncertainty > 1.6


def test_a_base_is_corrected_in_the_other_direction() -> None:
    """Henderson-Hasselbalch runs the opposite way for a base, and the sign is everything.

    Pyridine (pKaH 5.4) at pH 7.4 is essentially all neutral, so its logD must equal its clogP; the
    acid form would make it two log units too lipophobic.
    """
    result = logd_from_pka(_pka(_PYRIDINE, 5.4, site="base"), ph=7.4)
    molecule = Chem.MolFromSmiles(result.smiles)
    assert molecule is not None
    assert result.log_d == pytest.approx(Crippen.MolLogP(molecule), abs=0.05)


def test_a_polyprotic_acid_is_refused_rather_than_corrected_once() -> None:
    """One Henderson-Hasselbalch term cannot describe two ionised carboxyls, so it refuses.

    The predictor reports only the most acidic site, and the second carboxyl is also ionised at
    pH 7.4, so a single-term answer would be several times outside its printed uncertainty.
    """
    with pytest.raises(CalculationDomainError, match="2 acidic O-H/S-H site"):
        logd_from_pka(_pka(_SUCCINIC_ACID, 4.4), ph=7.4)


def test_a_polyprotic_acid_is_still_served_where_no_site_is_ionised() -> None:
    """A polyprotic acid is still served where no site is ionised.

    At pH 1 every site is less ionised than the most acidic one, so the omitted terms are
    negligible; refusing would exclude every polyol and sugar for no gain.
    """
    result = logd_from_pka(_pka(_SUCCINIC_ACID, 4.4), ph=1.0)
    assert result.log_d == pytest.approx(result.clogp, abs=0.01)


def test_an_amphoteric_molecule_is_refused_rather_than_treated_as_an_acid() -> None:
    """An amphoteric molecule (glycine) is refused rather than treated as an acid.

    The predictor takes the acid branch whenever an O-H is present, so the aliphatic-amine refusal
    must be consulted too, or the dominant amine ionisation goes unmodelled.
    """
    with pytest.raises(CalculationDomainError, match="amphoteric"):
        logd_from_pka(_pka(_GLYCINE, 2.3), ph=7.4)


def test_an_amide_beside_an_acid_does_not_read_as_amphoteric() -> None:
    """Paracetamol gets a logD: an amide nitrogen is not a basic centre.

    An amide's lone pair is conjugated into the carbonyl (protonated acetamide protonates on
    oxygen), so the amphoteric refusal must not fire on a phenol with an anilide.
    """
    result = logd_from_pka(_pka(_PARACETAMOL, 9.6), ph=7.4)
    assert result.log_d == pytest.approx(result.clogp, abs=0.01)


def test_a_monoprotic_acid_is_unchanged_by_the_multi_site_refusal() -> None:
    """Benzoic acid's logD is pinned so the multi-site refusal cannot shift a monoprotic result.

    A refusal that perturbed the molecules it was meant to leave alone would be worse than the bug
    it fixes, and only a pinned value can tell.
    """
    result = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784), ph=7.4)
    assert result.clogp == pytest.approx(1.3848, abs=1e-4)
    assert result.pka == pytest.approx(6.2784, abs=1e-3)
    assert result.log_d == pytest.approx(0.2315, abs=1e-3)
    # The uncertainty is the pKa residual carried through `dlogD/dpKa` and combined with Crippen's
    # RMSE; benzoic acid at pH 7.4 is 93 % ionised, so most of the pKa term survives.
    assert result.uncertainty == pytest.approx(1.6356, abs=1e-4)


# --- the site enumeration the domain check is exactly as good as ------------------------------


@pytest.mark.parametrize(
    ("smiles", "acidic", "basic", "why"),
    [
        (_BENZOIC_ACID, 1, 0, "one carboxyl O-H, no nitrogen"),
        (_SUCCINIC_ACID, 2, 0, "both carboxyls count, which is what makes it polyprotic"),
        (_GLYCINE, 1, 1, "a carboxyl and an aliphatic amine — the amphoteric case"),
        (_PARACETAMOL, 1, 0, "the phenol counts; the anilide nitrogen is not a base"),
        ("CC#N", 0, 0, "a nitrile's sp nitrogen has pKaH ~ -10: no aqueous pH protonates it"),
        ("c1cc[nH]c1", 0, 0, "pyrrole-type: the lone pair is the ring's aromatic sextet"),
        ("c1cnc[nH]1", 0, 1, "imidazole has one of each, so exactly one basic centre"),
        ("Nc1ccccc1", 0, 1, "aniline's bond to the ring is aromatic, not the amide C=O bond"),
        (
            "CS(=O)(=O)Nc1ccccc1",
            0,
            0,
            "a sulfonamide: its N-H is not an O-H/S-H so it is not an acid site here, and its "
            "nitrogen is not a base either — the honest answer is that this predictor has "
            "nothing to say about it",
        ),
        # Every other arm of the SMARTS table, one row each, added with the transcription so that
        # a pattern narrowed by one primitive fails here rather than in a chemist's logD.
        ("NC(=O)N", 0, 0, "urea: both nitrogens are conjugated into the same C=O"),
        ("COC(=O)NC", 0, 0, "a carbamate, which is the amide rule reached through an ester oxygen"),
        ("CC(=S)N", 0, 0, "a thioamide — the chalcogen arm of the rule is O *or* S"),
        ("CS(=O)N", 0, 0, "a sulfinamide: one S=O is enough, a sulfonamide's second is not needed"),
        ("O=C1CCCN1", 0, 0, "a lactam is an amide whose ring hides nothing from the rule"),
        ("N#CC#N", 0, 0, "two sp nitrogens are still two nitriles"),
        ("CC=NC", 0, 1, "an imine: a double bond is not a triple one and drains no lone pair"),
        ("CCN", 0, 1, "an aliphatic amine is enumerated here; the *predictor* is what refuses it"),
        ("Cn1ccnc1", 0, 1, "N-methylimidazole: the alkylated pyrrole-type N is still pyrrole-type"),
        ("NN", 0, 2, "hydrazine: two independent basic nitrogens, which is what polyprotic means"),
        ("C[N+](C)(C)C", 0, 0, "a quaternary ammonium has no valence and no charge-neutral pair"),
        ("[O-][N+](=O)c1ccccc1", 0, 0, "nitrobenzene's nitrogen is formally charged, not basic"),
        ("OCCO", 2, 0, "a diol is two acid sites — the polyprotic case the O-H count has to see"),
        ("CCS", 1, 0, "a thiol: the S in `[#8,#16]` is not decoration"),
        ("NO", 1, 1, "hydroxylamine is amphoteric on one heavy atom each"),
        ("Oc1ccncc1", 1, 1, "4-hydroxypyridine: a phenol and a pyridine-type N in one ring system"),
        (
            "CN1C=NC2=C1C(=O)N(C)C(=O)N2C",
            0,
            1,
            "caffeine: three amide-type nitrogens excluded and the imidazole's pyridine-type one "
            "kept — the whole table in one molecule",
        ),
    ],
)
def test_ionisable_sites_counts_only_sites_that_are_really_sites(
    smiles: str, acidic: int, basic: int, why: str
) -> None:
    """`_ionisable_sites` counts only sites with an available lone pair or acidic proton.

    Amide/carbamate/urea/sulfonamide, nitrile and pyrrole-type nitrogen have delocalized or
    unavailable lone pairs and are excluded, which keeps imidazole and paracetamol in the
    single-equilibrium domain. The enumeration mirrors the pKa predictor's (pure graph inspection,
    so no round trip on a refusal path) and is no better than it. The rules are the RDKit SMARTS
    patterns `_ACIDIC_SITE` and `_BASIC_SITE`; each row is one arm of them.
    """
    sites = ionisable_sites(smiles)
    assert (sites.acidic, sites.basic) == (acidic, basic), why
    assert sites.total == acidic + basic


def test_an_unparseable_molecule_is_named_rather_than_counted_as_zero_sites() -> None:
    """Zero sites would read as "a plain monoprotic acid" and pass the domain check silently."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        ionisable_sites("%%%not-a-mol%%%")
