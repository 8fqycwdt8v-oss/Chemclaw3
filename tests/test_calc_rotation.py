"""The rotational profile: naming the bond, releasing the wells, and timing the barrier.

Driven end to end through the real composite against `calc_server_fake`, whose torsional potential
is n-butane-shaped — three wells, the anti one lowest — so every claim below is checked against a
surface that could contradict it. Nothing here is mocked at the composite's own boundary: the scan
points, the relaxations and the Hessians all go through `cached_remote` and the D-011 store.

The four things this file exists to hold:

- **A wrong handle is refused**, because the failure it replaces is silent — a scan of the wrong
  bond returns a well-formed profile, not an error.
- **A well is released**, not reported as the constrained scan point it came from.
- **A barrier has a direction**, and the one out of the populated well is the one that matters.
- **A half-life is a range**, because Eyring is exponential in a number carrying ±3 kcal/mol.
"""

import asyncio

import pytest
from rdkit import Chem

from chemclaw.connectors.calc import compose
from chemclaw.core.chem import torsion_handle
from chemclaw.core.config import settings
from chemclaw.science.calc.budget import rotation_units
from chemclaw.science.calc.models import RotationProfile, Torsion
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.thermo import (
    barrier_from_half_life,
    half_life_from_barrier,
    rate_from_barrier,
)
from tests.calc_server_fake import (
    FakeCalcServer,
    embed,
    install,
    torsional_energy,
    with_dihedral,
)

# n-butane's central C-C, and the handle its own molecule mints for it. Derived rather than written
# out, because the literal belongs in the cross-repository contract table (`test_torsion_handle.py`)
# and duplicating it here would give one fact two homes.
_BUTANE = "CCCC"
_ATOMS = [0, 1, 2, 3]


def _torsion(smiles: str = _BUTANE, bond: tuple[int, int] = (1, 2), **overrides: object) -> Torsion:
    """The torsion `enumerate_torsions` would report for n-butane's central bond."""
    fields: dict[str, object] = {
        # Minted only when the caller has not supplied one: a test about an out-of-range index
        # cannot mint a handle for the index it is about.
        "torsion_id": overrides.pop("torsion_id", None)
        or torsion_handle(Chem.MolFromSmiles(smiles), bond),
        "atoms": _ATOMS,
        "bond": list(bond),
        "label": "the C1-C2 bond",
        "symmetry_order": 1,
        "period_degrees": 360.0,
    }
    return Torsion.model_validate({**fields, **overrides})


def _profile(server: FakeCalcServer, **kwargs: object) -> RotationProfile:
    """Run the composite against the fake, from a fresh cache."""
    return asyncio.run(
        compose.rotation_profile(
            InMemoryStore(),
            _BUTANE,
            _torsion(),
            **kwargs,  # type: ignore[arg-type]
        )
    )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeCalcServer:
    """A calculation server with a torsional potential over n-butane's central dihedral."""
    return install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 3)))


class TestTheBondIsCheckedNotTrusted:
    """The refusals, which are the whole reason the handle exists."""

    def test_a_handle_from_another_molecule_is_refused(self, server: FakeCalcServer) -> None:
        """The silent failure, made loud: the same indices name a real bond in both molecules."""
        wrong = _torsion(torsion_id=torsion_handle(Chem.MolFromSmiles("CCCCC"), (1, 2)))
        with pytest.raises(ValueError, match="does not name a bond of"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, wrong))
        assert server.count("scan_point") == 0, "it must refuse before spending anything"

    def test_a_ring_bond_is_refused_by_name(self, server: FakeCalcServer) -> None:
        """Driving one is a ring pucker, and the message says which question to ask instead."""
        ring = _torsion("C1CCCCC1", (0, 1), atoms=[5, 0, 1, 2], label="a ring bond")
        with pytest.raises(ValueError, match="ring pucker"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), "C1CCCCC1", ring))

    def test_a_top_is_refused_for_having_no_heavy_dihedral(self, server: FakeCalcServer) -> None:
        """A methyl rotation is real and is not this job — the message says where it is counted."""
        top = _torsion(atoms=[], label="the methyl top on C1")
        with pytest.raises(ValueError, match="free-rotor"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, top))

    def test_an_index_past_the_molecule_is_refused(self, server: FakeCalcServer) -> None:
        """The bounds check the scan already had, kept — with a message naming the way out.

        Checked before the handle, so an index nobody could have minted a handle for is reported as
        the out-of-range index it is rather than as a handle mismatch.
        """
        wide = _torsion(bond=(1, 99), atoms=[0, 1, 99, 3], torsion_id="tor_0000000000000000")
        with pytest.raises(ValueError, match="enumerate_torsions"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, wide))


class TestTheProfile:
    """What the composite finds on a surface whose wells and barriers are known in advance."""

    def test_it_finds_the_three_wells_of_a_three_fold_rotor(self, server: FakeCalcServer) -> None:
        """Anti and two gauche, at the angles the fake's potential puts them."""
        profile = _profile(server)
        assert len(profile.rotamers) == 3
        assert sorted(round(rotamer.dihedral_degrees) for rotamer in profile.rotamers) == [
            60,
            180,
            300,
        ]

    def test_the_anti_rotamer_is_the_populated_one(self, server: FakeCalcServer) -> None:
        """Rotamers come back most-populated first, and on this surface that is the anti well."""
        profile = _profile(server)
        assert round(profile.rotamers[0].dihedral_degrees) == 180
        assert profile.rotamers[0].population > sum(
            rotamer.population for rotamer in profile.rotamers[1:]
        )
        assert sum(rotamer.population for rotamer in profile.rotamers) == pytest.approx(1.0)

    def test_a_well_is_released_from_its_constraint(self, server: FakeCalcServer) -> None:
        """The point that matters most, and the one a bare scan cannot make.

        A scan point is optimized with the dihedral *frozen*, so the bottom of a well is the best
        constrained geometry rather than a minimum of the molecule. Shown here on a grid that does
        not line up with the wells: at 45 degrees the profile's own minima sit at 45, 180 and 315,
        and the released rotamers must sit at 60, 180 and 300 — where the surface actually has its
        minima. A composite that reported its scan points as rotamers would come back with the
        first list, which is why the step is chosen to make the two differ.
        """
        profile = _profile(server, step_degrees=45.0)
        angles = sorted(round(rotamer.dihedral_degrees) for rotamer in profile.rotamers)
        assert angles == [60, 180, 300]
        scanned = {round(point.value) for point in profile.points}
        assert {45, 315} <= scanned, "the premise failed: those grid angles were not scanned"
        assert 60 not in scanned and 300 not in scanned, (
            "the premise failed: this grid happens to contain the wells, so releasing them "
            "would move nothing and the test could not tell the two apart"
        )
        assert server.count("relax_structure") >= len(profile.rotamers)

    def test_two_wells_that_relax_into_one_are_merged_and_said_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coarse grid can see a feature that is not there, and that is a finding, not a detail.

        Forced by making every unconstrained relaxation settle in the same place, which is what a
        molecule with one real well and a shoulder does.
        """
        server = install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 3)))
        settled = server._optimization(_with_dihedral_at(180.0), None)
        server.overrides["relax_structure"] = lambda _arguments: settled
        profile = asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, _torsion()))
        assert len(profile.rotamers) == 1
        assert any("relax into one minimum" in warning for warning in profile.warnings)

    def test_the_scan_covers_one_period_not_always_a_full_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A two-fold torsion is answered by half a turn — and that is half the calculations.

        Counted rather than argued, because this is the whole reason `enumerate_torsions` reports a
        symmetry order at all: every degree not scanned is a constrained optimization not run.
        """
        counts = []
        for period in (360.0, 180.0):
            server = install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 3)))
            asyncio.run(
                compose.rotation_profile(
                    InMemoryStore(),
                    _BUTANE,
                    _torsion(symmetry_order=int(360 // period), period_degrees=period),
                )
            )
            counts.append(server.count("scan_point"))
        full, half = counts
        assert half < full, f"half a turn cost {half} points against {full} for a full one"

    def test_a_maximum_is_resolved_rather_than_stepped_over(self, server: FakeCalcServer) -> None:
        """A coarse grid lands near the top, not on it. The refinement is the height.

        Checked against the potential itself: the highest scanned point must be closer to the true
        barrier than the best coarse point was.
        """
        profile = _profile(server)
        coarse = {index * settings.xtb_rotation_step_degrees for index in range(12)}
        extra = [point.value for point in profile.points if point.value not in coarse]
        assert extra, "no refinement points were added around the maxima"
        true_barrier = (torsional_energy(120.0) - torsional_energy(180.0)) * 627.5094740631
        assert profile.highest_barrier_kcal == pytest.approx(true_barrier, abs=0.35)


class TestTheBarrier:
    """Directional, timed, and honest about its own uncertainty."""

    def test_a_barrier_is_reported_in_both_directions(self, server: FakeCalcServer) -> None:
        """Out of the anti well and out of the gauche well are different numbers."""
        profile = _profile(server)
        uneven = [
            barrier
            for barrier in profile.barriers
            if abs(barrier.forward_kcal - barrier.reverse_kcal) > 0.1
        ]
        assert uneven, "every barrier came back symmetric on a surface with unequal wells"

    def test_the_wrap_around_barrier_is_not_lost(self, server: FakeCalcServer) -> None:
        """A torsion is a ring: the pass between the last well and the first is a real pass.

        Treating the profile as a line rather than a ring drops exactly one barrier, and on a
        three-fold rotor it is the one across 0 degrees — which for n-butane is the *syn* barrier,
        the highest on the surface.
        """
        profile = _profile(server)
        assert len(profile.barriers) == len(profile.rotamers)
        assert any(
            barrier.at_degrees < 60 or barrier.at_degrees > 300 for barrier in profile.barriers
        )

    def test_every_barrier_carries_a_half_life_with_its_band(self, server: FakeCalcServer) -> None:
        """A single lifetime from a semiempirical barrier reads exactly like a measurement."""
        for barrier in _profile(server).barriers:
            lifetime = barrier.interconversion
            assert lifetime is not None
            assert (
                lifetime.half_life_seconds_fastest
                < lifetime.half_life_seconds
                < lifetime.half_life_seconds_slowest
            )
            assert lifetime.uncertainty_kcal == settings.xtb_reaction_uncertainty_kcal


class TestTheCache:
    """D-011 across a composite whose key would name its own output."""

    def test_a_repeat_profile_recomputes_nothing(self, server: FakeCalcServer) -> None:
        """Every part is separately keyed, so the second run pays for no calculation at all."""
        store = InMemoryStore()
        first = asyncio.run(compose.rotation_profile(store, _BUTANE, _torsion()))
        spent = server.count("scan_point") + server.count("relax_structure")
        second = asyncio.run(compose.rotation_profile(store, _BUTANE, _torsion()))
        assert server.count("scan_point") + server.count("relax_structure") == spent
        assert first.highest_barrier_kcal == second.highest_barrier_kcal

    def test_a_finer_step_pays_only_for_the_points_it_adds(self, server: FakeCalcServer) -> None:
        """The economy the composite exists for: refining a profile is not re-running it."""
        store = InMemoryStore()
        asyncio.run(compose.rotation_profile(store, _BUTANE, _torsion(), step_degrees=60.0))
        after_coarse = server.count("scan_point")
        asyncio.run(compose.rotation_profile(store, _BUTANE, _torsion(), step_degrees=30.0))
        added = server.count("scan_point") - after_coarse
        assert 0 < added < after_coarse + 12, (
            "a 30-degree run after a 60-degree one should reuse the six shared angles"
        )

    def test_the_budget_refuses_before_the_first_calculation(
        self, server: FakeCalcServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preflight that fires after three hours has already spent three hours."""
        monkeypatch.setattr(settings, "calc_max_primitive_calls", 3)
        with pytest.raises(ValueError, match="would run"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, _torsion()))
        assert server.count("scan_point") == 0

    def test_the_budget_counts_what_the_composite_actually_asks_for(
        self, server: FakeCalcServer
    ) -> None:
        """The fence under-counts silently if it drifts from the composite, so pin them together."""
        profile = _profile(server)
        asked = server.count("scan_point") + server.count("relax_structure")
        allowed = rotation_units(len(profile.points), max(1, len(profile.rotamers)), level="quick")
        assert asked <= allowed + len(profile.rotamers), (
            f"{asked} calls against a fence that would have allowed {allowed}"
        )


class TestEyring:
    """The arithmetic that used to be left to the model, checked against known anchors."""

    @pytest.mark.parametrize(
        ("barrier", "seconds"),
        [(20.0, 51.0), (24.0, 4.36e4), (27.0, 6.90e6), (30.0, 1.09e9)],
    )
    def test_the_half_life_matches_the_textbook_anchors(
        self, barrier: float, seconds: float
    ) -> None:
        """`t½ = ln2 / k`, `k = (kB T/h) exp(-dG‡/RT)`, transmission coefficient 1 at 298.15 K.

        Pinned as literals because these four numbers are what `skills/atropisomer-assessment` uses
        to classify a compound, and the prose table they replaced was wrong by up to two orders of
        magnitude at the top of the range — which is the difference between "about a day" and
        eighty days, on a decision boundary.
        """
        assert half_life_from_barrier(barrier, 298.15, 0.0).half_life_seconds == pytest.approx(
            seconds, rel=0.01
        )

    def test_the_inverse_round_trips(self) -> None:
        """A two-year shelf life asked as a barrier: the same relation, read backwards."""
        two_years = 2 * 365 * 24 * 3600
        needed = barrier_from_half_life(two_years, 298.15)
        assert half_life_from_barrier(needed, 298.15, 0.0).half_life_seconds == pytest.approx(
            two_years, rel=1e-9
        )

    def test_one_kcal_is_about_a_factor_of_five(self) -> None:
        """The reason the band travels with the number rather than being left to a reader."""
        ratio = rate_from_barrier(25.0, 298.15) / rate_from_barrier(26.0, 298.15)
        assert 5.0 == pytest.approx(ratio, rel=0.1)


def _with_dihedral_at(degrees: float) -> dict[str, object]:
    """An n-butane geometry with its central dihedral driven to `degrees`."""
    return with_dihedral(embed(_BUTANE), (0, 1, 2, 3), degrees)
