"""The conformer-ensemble cache key and its display cap (STO-3).

A CREST search is, by this module's own docstring, the most expensive single calculation in the
system. `ConformerSpec.max_members` decides how many of its findings a reader is shown — and it
sat in the cache key, so "show me 20 instead of 10" re-ran the search to obtain an answer already
stored. These tests pin the fix and, just as importantly, pin the settings that must *still*
recompute: a cache key that drops too much is worse than one that drops nothing.

No CREST binary needed — the claims are about keys and truncation, both of which are pure
functions over a spec and an ensemble.
"""

import asyncio

from chemclaw.science.calc.conformers import Conformer, ConformerEnsemble, ConformerSpec, truncated
from chemclaw.science.calc.store import InMemoryStore, run_cached
from chemclaw.science.calc.structure import Structure


def _butane() -> Structure:
    """A four-carbon skeleton — the molecule the ensemble tests in `test_xtb_cli` use."""
    return Structure(
        elements=[6, 6, 6, 6],
        positions=[[0.0, 0.0, 0.0], [1.5, 0.0, 0.0], [2.1, 1.4, 0.0], [3.6, 1.4, 0.0]],
        smiles="CCCC",
    )


def _ensemble(members: int) -> ConformerEnsemble:
    """An ensemble of `members` conformers with descending populations."""
    structure = _butane()
    return ConformerEnsemble(
        smiles="CCCC",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent=None,
        temperature_k=298.15,
        conformers=[
            Conformer(
                relative_kcal=round(0.1 * index, 3),
                population=1.0 / members,
                degeneracy=1,
                structure=structure,
            )
            for index in range(members)
        ],
        total_found=members,
        conformational_entropy_cal_per_mol_k=2.5,
        ensemble_correction_kcal=-0.75,
    )


def test_asking_for_more_members_is_the_same_calculation() -> None:
    """The fix: `max_members` no longer reaches the cache key.

    Before this, the second spec below was a miss and re-ran CREST — minutes of metadynamics to
    return conformers already sitting in the store.
    """
    structure = _butane()
    ten = ConformerSpec(max_members=10)
    twenty = ConformerSpec(max_members=20)
    assert ten.cache_key(structure) == twenty.cache_key(structure)


def test_everything_that_moves_the_search_still_moves_the_key() -> None:
    """Everything that moves the search must still move the key.

    The negative control: a key that dropped `effort` or `temperature_k` would serve a quick
    search's ensemble for an extensive one, or populations weighted at the wrong temperature.
    """
    structure = _butane()
    base = ConformerSpec()
    assert base.cache_key(structure) != ConformerSpec(effort="extensive").cache_key(structure)
    assert base.cache_key(structure) != ConformerSpec(temperature_k=350.0).cache_key(structure)
    assert base.cache_key(structure) != ConformerSpec(solvent="water").cache_key(structure)
    assert base.cache_key(structure) != ConformerSpec(search="tautomers").cache_key(structure)


def test_max_members_is_the_only_field_excluded_beyond_the_inherited_ones() -> None:
    """Stated as an assertion so widening the exclusion set is a deliberate, visible act.

    `unkeyed_fields` is the seam that made this fix possible without a second `cache_key`
    implementation; the risk it introduces is that it becomes an easy place to hide a field that
    really does matter. This test is the tripwire.
    """
    assert ConformerSpec.unkeyed_fields() == {"task", "method", "engine", "max_members"}


def test_truncation_keeps_the_ensembles_own_account_of_itself() -> None:
    """Cutting the list must not cut `total_found`, the populations, or the entropy.

    Those describe the whole ensemble. Truncating them would turn "the 10 that matter out of 47"
    into a quietly false claim that the search found 10 — and the conformational entropy, the term
    a single-conformer free energy is missing, is computed over every member by definition.
    """
    full = _ensemble(47)
    shown = truncated(full, 10)
    assert len(shown.conformers) == 10
    assert shown.total_found == 47
    assert shown.conformational_entropy_cal_per_mol_k == full.conformational_entropy_cal_per_mol_k
    assert shown.ensemble_correction_kcal == full.ensemble_correction_kcal
    # The lowest member survives truncation — it is what every downstream single-structure task
    # consumes (`ConformerEnsemble.lowest`).
    assert shown.conformers[0].relative_kcal == 0.0


def test_truncation_of_a_short_ensemble_returns_it_unchanged() -> None:
    """Asking for more than exists is not an error and allocates nothing."""
    full = _ensemble(3)
    assert truncated(full, 10) is full


def test_a_wider_view_of_a_cached_ensemble_costs_nothing() -> None:
    """End to end over the store: one search, two views of it.

    Exercises `run_cached` directly rather than `run_cached_ensemble`, because the latter needs
    `calc_version()` — which shells out to `crest --version` and is unavailable here. The property
    under test is the store interaction, and this is it.
    """

    async def _run() -> None:
        store = InMemoryStore()
        calls = 0

        def search() -> ConformerEnsemble:
            nonlocal calls
            calls += 1
            return _ensemble(47)

        # Both specs derive the same key (asserted above), so the second call is a hit.
        key = ConformerSpec(max_members=10).cache_key(_butane())
        narrow, narrow_cached = await run_cached(store, key, search, ConformerEnsemble)
        wide, wide_cached = await run_cached(store, key, search, ConformerEnsemble)

        assert calls == 1
        assert (narrow_cached, wide_cached) == (False, True)
        assert len(truncated(narrow, 10).conformers) == 10
        assert len(truncated(wide, 20).conformers) == 20
        # The store holds the whole ensemble, which is what makes the wider view free.
        assert len(wide.conformers) == 47

    asyncio.run(_run())
