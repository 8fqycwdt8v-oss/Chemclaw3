"""The inline-vs-durable router predicts the right side of the line (xTB plan X3/X4).

The estimate is a router, not a promise, so these tests assert *orderings and
thresholds* — the only properties it is used for — against the calls that were actually
timed while building the phase.
"""

import pytest

from calc.xtb_cost import (
    atom_count,
    exceeds_inline_budget,
    reaction_seconds,
    scan_seconds,
    species_seconds,
)
from chemclaw.config import settings

_ESTERIFICATION = ["CC(=O)O", "CCO", "CCOC(C)=O", "O"]


def test_a_hessian_costs_more_than_an_optimization() -> None:
    """6N gradient evaluations against a few dozen — the reason `quick` exists."""
    assert species_seconds(10, hessian=True) > species_seconds(10, hessian=False)


def test_cost_grows_with_molecule_size() -> None:
    """Monotonic in atoms, which is the only shape the router relies on."""
    sizes = [species_seconds(n, hessian=True) for n in (3, 9, 20, 60)]
    assert sizes == sorted(sizes)


def test_atom_count_includes_hydrogens() -> None:
    """Cost scales in *all* atoms; a heavy-atom count would underestimate by ~half."""
    assert atom_count("CCO") == 9
    assert atom_count("O") == 3


def test_the_measured_calls_land_on_the_side_they_were_measured_on() -> None:
    """Calibration check against real timings taken while building X3/X4.

    The esterification took 4.6 s and must stay inline; the same reaction screened
    across five solvents is ~25 s and must not. These are the two cases that decide
    whether a chemist waits, so they are pinned rather than left to a comment.
    """
    inline = reaction_seconds(_ESTERIFICATION, hessian=True)
    screen = reaction_seconds(_ESTERIFICATION, hessian=True, repeats=6)
    assert not exceeds_inline_budget(inline)
    assert exceeds_inline_budget(screen)
    assert inline == pytest.approx(4.6, abs=3.0)


@pytest.mark.parametrize(
    ("atoms", "measured"),
    [(9, 0.46), (31, 14.6), (33, 19.0), (63, 501.1), (76, 314.9), (118, 1559.6)],
    ids=["ethanol", "naproxen", "ibuprofen", "sildenafil", "atorvastatin-core", "erythromycin"],
)
def test_the_model_tracks_the_measured_drug_sized_timings(atoms: int, measured: float) -> None:
    """Optimize-plus-Hessian timings actually measured on this stack.

    The regression that matters. The first cost model was fitted on 3-14 atom test
    molecules, came out with an exponent of 1.7, and under-predicted the 76-atom
    substrate by nearly sevenfold — which in production means a chat sitting silent for
    five minutes on a request the router thought was cheap.

    The tolerance is deliberately wide, because the scatter is real rather than fitting
    error: sildenafil (63 atoms) costs more than the atorvastatin core (76), since a
    heteroatom-dense conjugated system carries more basis functions per atom and
    converges its SCF harder. No function of atom count removes that, which is precisely
    why the prediction is used against a threshold and never quoted as a countdown.
    """
    ratio = species_seconds(atoms, hessian=True) / measured
    assert 0.4 <= ratio <= 2.5


def test_a_drug_sized_molecule_always_defers() -> None:
    """The stated workload is 200-800 Da, and none of it belongs in a conversation turn."""
    for smiles in ("CC(C)Cc1ccc(cc1)C(C)C(=O)O", "COc1ccc2cc(ccc2c1)C(C)C(=O)O"):
        assert exceeds_inline_budget(reaction_seconds([smiles], hessian=True))


def test_a_long_scan_on_a_real_molecule_defers() -> None:
    """Seven points on butane took 4.2 s inline; a full 24-point profile should not."""
    assert not exceeds_inline_budget(scan_seconds("CCCC", 7))
    assert exceeds_inline_budget(scan_seconds("CCCC", settings.xtb_scan_max_points))


def test_the_budget_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing is a deployment decision, not a constant baked into the code."""
    from chemclaw.config import settings as live

    monkeypatch.setattr(live, "xtb_inline_budget_seconds", 0.001)
    assert exceeds_inline_budget(species_seconds(3, hessian=False))
