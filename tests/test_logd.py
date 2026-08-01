"""Behavioral tests for the pH-dependent logD calculator (D-092).

Runs real GFN2-xTB (via the reused pKa predictor); asserts the Henderson-Hasselbalch direction
(more acid protonated at low pH → higher logD) and that it inherits `chemclaw.science.calc.pka`'s
domain limits.
"""

import asyncio

import pytest

from chemclaw.science.calc.logd import LogdInput, predict_logd
from chemclaw.science.calc.store import InMemoryStore

_BENZOIC_ACID = "OC(=O)c1ccccc1"
_SUCCINIC_ACID = "OC(=O)CCC(=O)O"  # diprotic: both carboxyls ionised at pH 7.4
_GLYCINE = "NCC(=O)O"  # amphoteric: a carboxyl and an aliphatic amine
_PARACETAMOL = "CC(=O)Nc1ccc(O)cc1"  # a phenol plus an amide N, which is *not* a base


def test_logd_defaults_to_configured_ph() -> None:
    """Omitting `ph` uses `settings.logd_default_ph` (7.4), not an arbitrary constant."""
    from chemclaw.core.config import settings

    async def _run() -> None:
        store = InMemoryStore()
        result = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID))
        assert result.ph == settings.logd_default_ph

    asyncio.run(_run())


def test_logd_increases_as_ph_drops_below_pka() -> None:
    """Below the pKa the acid is mostly neutral: logD rises toward logP as pH falls."""

    async def _run() -> None:
        store = InMemoryStore()
        physiological = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=7.4))
        acidic = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=1.0))
        assert acidic.log_d > physiological.log_d
        # Far below the pKa, logD approaches logP (the fully neutral limit).
        assert acidic.log_d == pytest.approx(acidic.clogp, abs=0.05)

    asyncio.run(_run())


def test_logd_reports_pka_uncertainty() -> None:
    """The pKa model's uncertainty is surfaced, not silently dropped."""

    async def _run() -> None:
        store = InMemoryStore()
        result = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID))
        assert result.uncertainty > 0

    asyncio.run(_run())


def test_logd_rejects_a_molecule_with_no_acidic_site() -> None:
    """No O-H/S-H site → the underlying pKa error propagates unchanged (gate G4)."""

    async def _run() -> None:
        store = InMemoryStore()
        with pytest.raises(ValueError, match="no acidic"):
            await predict_logd(store, LogdInput(smiles="CCCCCC"))

    asyncio.run(_run())


def test_logd_reuses_the_cached_pka() -> None:
    """A second logD call at a different pH does not recompute the xTB pKa."""

    async def _run() -> None:
        from chemclaw.science.calc.pka import PkaInput, run_cached_pka

        store = InMemoryStore()
        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=7.0))
        pka_before, cached_before = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_before is True  # already computed by the logD call above

        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=3.0))
        pka_after, cached_after = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_after is True
        assert pka_after.pka == pka_before.pka

    asyncio.run(_run())


def test_a_base_is_corrected_in_the_other_direction() -> None:
    """Henderson-Hasselbalch runs the opposite way for a base, and the sign is everything.

    A cross-branch regression, invisible to either side alone. `chemclaw.science.calc.logd` was
    written when
    `chemclaw.science.calc.pka` covered acids only, so it hard-coded the acid form
    `logD = clogP - log10(1 + 10**(pH - pKa))`. X11 widened the predictor to aromatic and
    aryl nitrogen, and pyridine — which previously *raised* — began flowing into that
    formula as though it were an acid.

    Measured: pyridine (pKaH 5.4) at pH 7.4 came out at -0.92 against a clogP of 1.08, two
    full log units too lipophobic, and nothing raised. A base two units *below* the working
    pH is essentially all neutral, so its logD must be its clogP — which is the assertion.
    """

    async def _run() -> None:
        from rdkit import Chem
        from rdkit.Chem import Crippen

        result = await predict_logd(InMemoryStore(), LogdInput(smiles="c1ccncc1", ph=7.4))
        mol = Chem.MolFromSmiles(result.smiles)
        assert mol is not None
        assert result.pka < 6.5  # a weak base, well below the working pH
        assert result.log_d == pytest.approx(Crippen.MolLogP(mol), abs=0.05)

    asyncio.run(_run())


def test_an_aliphatic_amine_is_refused_rather_than_given_a_logd() -> None:
    """The refusal propagates: no pKa means no pH correction, so no logD (gate G4).

    `chemclaw.science.calc.pka` declines aliphatic amines because it cannot rank them at all, and a
    logD
    built on a number that does not exist would be a plausible-looking product of two
    guesses rather than one.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="aliphatic nitrogen"):
            await predict_logd(InMemoryStore(), LogdInput(smiles="C1CCNCC1", ph=7.4))

    asyncio.run(_run())


def test_a_polyprotic_acid_is_refused_rather_than_corrected_once() -> None:
    """One Henderson-Hasselbalch term cannot describe two ionised carboxyls (gate G4).

    Measured on the single-term code: succinic acid at pH 7.4 returned **-1.48 ± 1.6** against a
    true logD near **-5**. `predict_pka` reports the most acidic site only, so the second carboxyl
    — ionised at this pH too — contributed nothing, and the error it left is three to four times
    the uncertainty printed beside it. The second pKa is not obtainable from this predictor at
    all, so there is no number to correct with and the honest output is none.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="2 acidic O-H/S-H site"):
            await predict_logd(InMemoryStore(), LogdInput(smiles=_SUCCINIC_ACID, ph=7.4))

    asyncio.run(_run())


def test_a_polyprotic_acid_is_still_served_where_no_site_is_ionised() -> None:
    """The refusal is about ionisation, not about site count — the distinction is the whole rule.

    At pH 1 both of succinic acid's carboxyls are neutral, and `predict_pka` reports the *most*
    acidic of them: every other site is therefore less ionised still, so the one term it omits is
    bounded and negligible. Refusing here instead would take out every polyol and sugar (O-H,
    pKa ~15, never ionised in the pH window this calculator serves) for no gain in honesty.
    """

    async def _run() -> None:
        result = await predict_logd(InMemoryStore(), LogdInput(smiles=_SUCCINIC_ACID, ph=1.0))
        assert result.log_d == pytest.approx(result.clogp, abs=0.01)

    asyncio.run(_run())


def test_an_amphoteric_molecule_is_refused_rather_than_treated_as_an_acid() -> None:
    """Glycine must not slip past the aliphatic-amine refusal by also carrying a carboxyl.

    The bypass, measured: `predict_pka` takes the acid branch whenever *any* O-H is present, so
    glycine never reached the amine branch that refuses piperidine three tests above, and logD
    came back at **-2.81** with no error at all — one ionisation term, applied to the carboxyl,
    with the amine that dominates glycine's speciation at pH 7.4 unmodelled and unmentioned. The
    refusal that already existed was not weak here, it was simply never consulted.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="amphoteric"):
            await predict_logd(InMemoryStore(), LogdInput(smiles=_GLYCINE, ph=7.4))

    asyncio.run(_run())


def test_an_amide_beside_an_acid_does_not_read_as_amphoteric() -> None:
    """Paracetamol gets a logD: its nitrogen is an amide, and an amide nitrogen is not a base.

    The amphoteric refusal above is only honest if it fires on molecules that are actually
    amphoteric. `calc.pka`'s site enumeration counted any neutral nitrogen with free valence, so
    paracetamol — a phenol with an anilide, no basic centre anywhere — was refused a logD along
    with glycine. One of the most-screened molecules in pharma is not an acceptable casualty of
    a domain gate, and the fix belonged in the enumeration rather than here: an amide's lone
    pair is conjugated into the carbonyl and protonated acetamide (pKaH ~ -0.5) protonates on
    the oxygen.

    The phenol is far above the working pH, so logD is its logP — which is the point: this is a
    plain monoprotic acid and always was.
    """

    async def _run() -> None:
        result = await predict_logd(InMemoryStore(), LogdInput(smiles=_PARACETAMOL, ph=7.4))
        assert result.pka > 9.0  # the phenol, essentially neutral at pH 7.4
        assert result.log_d == pytest.approx(result.clogp, abs=0.01)

    asyncio.run(_run())


def test_a_monoprotic_acid_is_unchanged_by_the_multi_site_refusal() -> None:
    """The exact pre-fix numbers for benzoic acid, pinned so the new gate cannot shift them.

    Values recorded from the shipped single-term calculator before the domain check existed. A
    refusal that also perturbed the molecules it was meant to leave alone would be a worse bug
    than the one it fixes, and only a pinned value can tell the difference.
    """

    async def _run() -> None:
        result = await predict_logd(InMemoryStore(), LogdInput(smiles=_BENZOIC_ACID, ph=7.4))
        assert result.clogp == pytest.approx(1.3848, abs=1e-4)
        assert result.pka == pytest.approx(6.2784, abs=1e-3)
        assert result.log_d == pytest.approx(0.2315, abs=1e-3)
        assert result.uncertainty == pytest.approx(1.6)

    asyncio.run(_run())
