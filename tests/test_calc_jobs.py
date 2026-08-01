"""The `calc` bundle's durable job activity threads its request through to the science (D-118).

`run_xtb_calculation` is the only caller of `compute_reaction_energy`/`compare_solvent_effects` on
the durable path, and it dispatches on a `XtbJobSpec` member whose fields it copies across by hand.
So a field that exists in the science signature and not in the spec — or exists in both and is not
copied — is invisible: the job runs, returns a well-formed result, and quietly answers a smaller
question than the one that was asked.

That is exactly what happened to `symmetry_numbers`. `compute_reaction_energy` grew it as the input
a free energy is not computed without, `ReactionJobSpec`/`SolventScreenJobSpec` did not, and both
call sites were positional up to `level` — so nothing broke and the durable
`compute_reaction_energy` and `compare_solvents` jobs simply stopped reporting a free energy at
all. A chemist who ran the job instead of the inline tool got ΔE, and no indication that the ΔG
they asked for had been withheld for a reason they could have fixed.

Real GFN2 calculations against an in-memory store, on the smallest reaction that can show the
effect: a stub of the science would pin only that this module calls a function. Diatomics keep
that honest and still cheap — every Hessian here is over two atoms.
"""

import asyncio
from collections.abc import Iterator

import pytest
from temporalio import activity

from chemclaw.connectors.calc import activities
from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.specs import (
    ReactionJobSpec,
    SolventScreenJobSpec,
    XtbJobInput,
    xtb_job_key,
)
from chemclaw.science.calc.store import InMemoryStore

# H2 + Cl2 -> 2 HCl. Every species is a closed-shell diatomic, so a Hessian is a dozen single
# points and the whole file stays inside this repo's per-test budget — while still being the shape
# that matters: the two homonuclear reactants are D∞h (sigma=2) and the product is C∞v (sigma=1), so
# sigma does *not* cancel across the arrow and the map has to reach the calculation to change
# anything. The three distinct values also pin the map key by key rather than as a whole.
_REACTANTS = ["[H][H]", "ClCl"]
_PRODUCTS = ["Cl", "Cl"]
_SIGMAS = {"[H][H]": 2, "ClCl": 2, "Cl": 1}


@pytest.fixture(scope="module")
def store() -> InMemoryStore:
    """One store for the module, so the species shared between these jobs are computed once.

    The same discipline the jobs themselves rely on (D-011), and here it is also what keeps a
    Hessian-bearing test file to seconds.
    """
    return InMemoryStore()


@pytest.fixture(autouse=True)
def _durable_context(monkeypatch: pytest.MonkeyPatch, store: InMemoryStore) -> Iterator[None]:
    """Run the activity outside Temporal: its own store, and a heartbeat that goes nowhere.

    `activity.heartbeat` raises outside an activity context, and it is passed down as the progress
    callback, so this is what makes the real function callable at all from a test.
    """
    monkeypatch.setattr(activities, "default_store", lambda: store)
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)
    yield


def _run(spec: ReactionJobSpec | SolventScreenJobSpec) -> XtbJobResult:
    """Run one durable xTB job to completion, as its worker would."""
    return asyncio.run(activities.run_xtb_calculation(XtbJobInput(spec=spec)))


def test_a_reaction_job_that_states_its_symmetry_numbers_gets_a_free_energy() -> None:
    """The passthrough, proven by the number that only exists when it works.

    `symmetry_number` per species is asserted alongside ΔG because it pins the map *key by key*:
    a passthrough that dropped the values and passed an empty map would still yield None, but one
    that mismatched Cl2's 2 onto HCl would not be visible in ΔG alone.
    """
    spec = ReactionJobSpec(reactants=_REACTANTS, products=_PRODUCTS, symmetry_numbers=_SIGMAS)
    result = _run(spec)
    reaction = result.reaction
    assert reaction is not None
    assert reaction.delta_g_kcal is not None
    assert [entry.symmetry_number for entry in reaction.species] == [2, 2, 1, 1]
    assert not [w for w in reaction.warnings if "symmetry number" in w]
    # The summary is derived from the result, and it is the one line a completion push-back
    # carries — so a withheld ΔG must not be announced as one.
    assert "dG" in result.summary


def test_a_reaction_job_without_symmetry_numbers_withholds_the_free_energy() -> None:
    """Omitting them is honest, not free: ΔE and ΔH stand, ΔG does not, and the warning says why.

    The state that must never come back is a third one — a ΔG computed at sigma=1 for symmetric
    species and reported as an ordinary number.
    """
    result = _run(ReactionJobSpec(reactants=_REACTANTS, products=_PRODUCTS))
    reaction = result.reaction
    assert reaction is not None
    assert reaction.delta_g_kcal is None
    assert reaction.delta_h_kcal is not None
    assert all(entry.symmetry_number is None for entry in reaction.species)
    (warning,) = [w for w in reaction.warnings if "symmetry number" in w]
    assert "ClCl" in warning and "[H][H]" in warning
    assert "dE" in result.summary


def test_a_solvent_screen_threads_the_same_map_through_every_solvent() -> None:
    """One map covers the whole screen, and without it the ranking silently drops to ΔE.

    The screen is the call where the loss was widest: it runs the reaction once per medium, so a
    dropped map costs a free energy in every one of them at once.
    """
    spec = SolventScreenJobSpec(
        reactants=_REACTANTS, products=_PRODUCTS, solvents=["water"], symmetry_numbers=_SIGMAS
    )
    result = _run(spec)
    comparison = result.solvents
    assert comparison is not None
    # Gas phase plus the one solvent, each with a free energy of its own.
    assert [effect.solvent for effect in comparison.effects].count(None) == 1
    assert all(effect.delta_g_kcal is not None for effect in comparison.effects)
    assert not [w for w in comparison.warnings if "symmetry number" in w]


def test_the_symmetry_numbers_are_part_of_a_jobs_identity() -> None:
    """Two jobs differing only in sigma are different calculations, so they need different keys.

    `xtb_job_key` hashes the spec so that submitting the same request twice returns the existing
    run. A field the key does not see would make a correctly-stated re-run return the earlier
    run's withheld ΔG — the deduplication turning into a wrong answer.
    """
    stated = XtbJobInput(
        spec=ReactionJobSpec(reactants=_REACTANTS, products=_PRODUCTS, symmetry_numbers=_SIGMAS)
    )
    omitted = XtbJobInput(spec=ReactionJobSpec(reactants=_REACTANTS, products=_PRODUCTS))
    assert xtb_job_key(stated) != xtb_job_key(omitted)
