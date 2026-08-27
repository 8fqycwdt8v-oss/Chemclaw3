"""The local half of logD: a Crippen sum, one Henderson-Hasselbalch term, and the domain it holds.

logD was decomposed rather than shipped (`D-2026-08-16-the-physics-leaves-the-cache-stays`): its
expensive half is a *cached* pKa on the calculation server, and everything else is RDKit. So this
file drives `logd_from_pka` with a `PkaResult` the caller supplies, which is what the tool does with
whatever the cache served it — no server, no SCF, and the arithmetic under test is exactly the
arithmetic that runs in production.

What is asserted is the part that stayed and the part that is easy to get silently wrong: the
**direction** of the correction (it runs the opposite way for a base), the **domain** (one term
cannot describe two ionised sites), and the site **enumeration** that decides which of those a
molecule is. The pKa values themselves are the other repository's business, and the ones used below
are the numbers the shipped predictor produced for these molecules, so the pinned outputs are the
pinned outputs of the whole composition.
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
    """LogD's error bar is Crippen's RMSE and the pKa's, each carried through its own derivative.

    `logD = clogP - log10(1 + 10**(±(pH - pKa)))`, so `dlogD/dclogP = 1` and `dlogD/dpKa` is the
    **ionised fraction** — between 0 and 1, and near zero for exactly the molecules this
    composition is allowed to serve, since `_require_a_single_equilibrium` refuses a polyprotic
    molecule above `logd_negligible_ionised_fraction`. Copying the pKa's residual across is
    therefore neither a propagation nor, in general, the dominant term.

    Pyridine at pH 7.4 is the measured case: 0.67 % ionised, so the pKa contributes 0.0094 log
    units of the bar it was reported as the whole of.
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

    A cross-branch regression, invisible to either side alone. This arithmetic was written when the
    pKa predictor covered acids only, so it hard-coded the acid form
    `logD = clogP - log10(1 + 10**(pH - pKa))`. X11 widened the predictor to aromatic and aryl
    nitrogen, and pyridine — which previously *raised* — began flowing into that formula as though
    it were an acid.

    Measured: pyridine (pKaH 5.4) at pH 7.4 came out at -0.92 against a clogP of 1.08, two full log
    units too lipophobic, and nothing raised. A base two units *below* the working pH is essentially
    all neutral, so its logD must be its clogP — which is the assertion.
    """
    result = logd_from_pka(_pka(_PYRIDINE, 5.4, site="base"), ph=7.4)
    molecule = Chem.MolFromSmiles(result.smiles)
    assert molecule is not None
    assert result.log_d == pytest.approx(Crippen.MolLogP(molecule), abs=0.05)


def test_a_polyprotic_acid_is_refused_rather_than_corrected_once() -> None:
    """One Henderson-Hasselbalch term cannot describe two ionised carboxyls (gate G4).

    Measured on the single-term code: succinic acid at pH 7.4 returned **-1.48 ± 1.6** against a
    true logD near **-5**. The predictor reports the most acidic site only, so the second carboxyl —
    ionised at this pH too — contributed nothing, and the error it left is three to four times the
    uncertainty printed beside it. The second pKa is not obtainable from that predictor at all, so
    there is no number to correct with and the honest output is none.
    """
    with pytest.raises(CalculationDomainError, match="2 acidic O-H/S-H site"):
        logd_from_pka(_pka(_SUCCINIC_ACID, 4.4), ph=7.4)


def test_a_polyprotic_acid_is_still_served_where_no_site_is_ionised() -> None:
    """The refusal is about ionisation, not about site count — the distinction is the whole rule.

    At pH 1 both of succinic acid's carboxyls are neutral, and the predictor reports the *most*
    acidic of them: every other site is therefore less ionised still, so the one term it omits is
    bounded and negligible. Refusing here instead would take out every polyol and sugar (O-H,
    pKa ~15, never ionised in the pH window this calculator serves) for no gain in honesty.
    """
    result = logd_from_pka(_pka(_SUCCINIC_ACID, 4.4), ph=1.0)
    assert result.log_d == pytest.approx(result.clogp, abs=0.01)


def test_an_amphoteric_molecule_is_refused_rather_than_treated_as_an_acid() -> None:
    """Glycine must not slip past the aliphatic-amine refusal by also carrying a carboxyl.

    The bypass, measured: the predictor takes the acid branch whenever *any* O-H is present, so
    glycine never reached the amine branch that refuses piperidine, and logD came back at **-2.81**
    with no error at all — one ionisation term, applied to the carboxyl, with the amine that
    dominates glycine's speciation at pH 7.4 unmodelled and unmentioned. The refusal that already
    existed was not weak here, it was simply never consulted.
    """
    with pytest.raises(CalculationDomainError, match="amphoteric"):
        logd_from_pka(_pka(_GLYCINE, 2.3), ph=7.4)


def test_an_amide_beside_an_acid_does_not_read_as_amphoteric() -> None:
    """Paracetamol gets a logD: its nitrogen is an amide, and an amide nitrogen is not a base.

    The amphoteric refusal above is only honest if it fires on molecules that are actually
    amphoteric. The site enumeration counted any neutral nitrogen with free valence, so paracetamol
    — a phenol with an anilide, no basic centre anywhere — was refused a logD along with glycine.
    One of the most-screened molecules in pharma is not an acceptable casualty of a domain gate,
    and the fix belonged in the enumeration: an amide's lone pair is conjugated into the carbonyl,
    and protonated acetamide (pKaH ~ -0.5) protonates on the oxygen.
    """
    result = logd_from_pka(_pka(_PARACETAMOL, 9.6), ph=7.4)
    assert result.log_d == pytest.approx(result.clogp, abs=0.01)


def test_a_monoprotic_acid_is_unchanged_by_the_multi_site_refusal() -> None:
    """The exact pre-fix numbers for benzoic acid, pinned so nothing since can shift them.

    Values recorded from the shipped single-term calculator before the domain check existed, and
    they survived the move: the Crippen descriptor and the Henderson-Hasselbalch term are both still
    computed in this process, so the whole composition still lands on `0.2315` given the pKa the
    predictor produced. A refusal — or a migration — that perturbed the molecules it was meant to
    leave alone would be a worse bug than the one it fixes, and only a pinned value can tell.
    """
    result = logd_from_pka(_pka(_BENZOIC_ACID, 6.2784), ph=7.4)
    assert result.clogp == pytest.approx(1.3848, abs=1e-4)
    assert result.pka == pytest.approx(6.2784, abs=1e-3)
    assert result.log_d == pytest.approx(0.2315, abs=1e-3)
    # The *uncertainty* deliberately did move: it was the pKa residual copied across, and is now
    # that residual carried through `dlogD/dpKa` and combined with Crippen's own RMSE. Benzoic acid
    # at pH 7.4 is 93 % ionised, so almost all of the pKa term survives the derivative and the bar
    # grows rather than shrinks — see `test_a_fully_ionised_acid_carries_both_terms_in_quadrature`.
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
    ],
)
def test_ionisable_sites_counts_only_sites_that_are_really_sites(
    smiles: str, acidic: int, basic: int, why: str
) -> None:
    """The enumeration the amphoteric and polyprotic refusals both read, and its exclusions.

    Free valence says a lone pair *exists*; it does not say the pair is available. Three classes
    have one that is not — amide/carbamate/urea/sulfonamide, nitrile, and pyrrole-type aromatic
    nitrogen — and each exclusion is a delocalized or unavailable lone pair rather than a
    convenience. Counting them instead is what put imidazole (one basic centre) and paracetamol (no
    basic centre) outside the single-equilibrium domain they belong in.

    **Duplicated across the repository boundary now**, deliberately: this mirrors the enumeration
    the pKa predictor runs before any xTB, it is pure graph inspection, and asking the server for it
    would cost a round trip on a refusal path. It is therefore exactly as good as that enumeration
    and no better, which is what the docstring says and what this table pins.
    """
    sites = ionisable_sites(smiles)
    assert (sites.acidic, sites.basic) == (acidic, basic), why
    assert sites.total == acidic + basic


def test_an_unparseable_molecule_is_named_rather_than_counted_as_zero_sites() -> None:
    """Zero sites would read as "a plain monoprotic acid" and pass the domain check silently."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        ionisable_sites("%%%not-a-mol%%%")
