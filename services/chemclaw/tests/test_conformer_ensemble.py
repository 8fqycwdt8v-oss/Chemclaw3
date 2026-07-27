"""Behavioral tests for the Boltzmann-weighted conformer ensemble (D-092).

Runs real GFN2-xTB single points on a small multi-conformer ensemble. Asserts the physics
(populations sum to one, the weighted energy sits within the sampled range), the pruning
contract, and gate-G4 propagation of bad input — mirrors the depth of `test_xtb.py`/`test_pka.py`.
"""

import pytest

from calc.conformer_ensemble import ConformerEnsembleInput, compute_conformer_ensemble


def test_populations_sum_to_one() -> None:
    """The Boltzmann populations of every surviving conformer always sum to 1."""
    result = compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCCCO"))
    assert result.conformers  # at least one conformer survived
    assert sum(c.boltzmann_population for c in result.conformers) == pytest.approx(1.0, abs=1e-9)


def test_weighted_energy_within_sampled_range() -> None:
    """The Boltzmann-weighted energy is a convex combination, so it cannot beat the lowest one."""
    result = compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCCCO"))
    assert result.lowest_energy_hartree <= result.boltzmann_weighted_energy_hartree


def test_lowest_energy_conformer_has_zero_relative_energy() -> None:
    """The lowest-energy surviving conformer's own relative energy is exactly zero."""
    result = compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCCCO"))
    lowest = min(result.conformers, key=lambda c: c.relative_energy_kcal)
    assert lowest.relative_energy_kcal == pytest.approx(0.0, abs=1e-6)
    # ...and it carries the largest population (nothing else can outweigh the lowest energy).
    assert lowest.boltzmann_population == max(c.boltzmann_population for c in result.conformers)


def test_evaluated_never_exceeds_generated() -> None:
    """The MMFF energy-window prune only ever removes conformers, never adds any."""
    result = compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCCCO"))
    assert result.n_conformers_evaluated <= result.n_conformers_generated
    assert result.n_conformers_evaluated == len(result.conformers)


def test_ensemble_size_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A smaller configured ensemble size embeds fewer conformers (ETKDG's own upper bound)."""
    from chemclaw.config import settings

    monkeypatch.setattr(settings, "conformer_ensemble_size", 3)
    result = compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCCCO"))
    assert result.n_conformers_generated <= 3


def test_charge_mismatch_raises() -> None:
    """A declared charge contradicting the SMILES formal charge fails fast (gate G4)."""
    with pytest.raises(ValueError, match="does not match the formal charge"):
        compute_conformer_ensemble(ConformerEnsembleInput(smiles="CCO", charge=1))


def test_open_shell_species_raises() -> None:
    """An odd-electron species is rejected — GFN2-xTB here is closed-shell only."""
    with pytest.raises(ValueError, match="open-shell"):
        compute_conformer_ensemble(ConformerEnsembleInput(smiles="[CH3]"))


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast rather than returning a bogus ensemble."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        compute_conformer_ensemble(ConformerEnsembleInput(smiles="%%%not-a-mol%%%"))
