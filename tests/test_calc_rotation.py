"""The rotational profile: naming the bond, releasing the wells, and timing the barrier.

Driven end to end through the real composite against `calc_server_fake`'s n-butane-shaped
three-well torsion, with every primitive going through `cached_remote` and the D-011 store. Held
here: a wrong handle is refused (a wrong-bond scan is otherwise a silent well-formed profile), a
well is released from its constraint, a barrier has a direction, and a half-life is a range.
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
    half_life_from_barrier,
    rate_from_barrier,
)
from tests.calc_server_fake import (
    FakeCalcServer,
    _structure_id,
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


def _profile(
    server: FakeCalcServer, *, bond: Torsion | None = None, **kwargs: object
) -> RotationProfile:
    """Run the composite against the fake, from a fresh cache.

    `server` is taken and unused: it is the fixture that installs the fake, and naming it at each
    call site is what makes the dependency visible.
    """
    del server
    return asyncio.run(
        compose.rotation_profile(
            InMemoryStore(),
            _BUTANE,
            bond or _torsion(),
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
        """A methyl rotation is real and is not this job — the message says where it is counted.

        Uses n-butane's terminal C-C, which is what a methyl top is; an empty `atoms` alone must not
        produce the methyl sentence (see `TestARotorWhoseEndCarriesOnlyHydrogens`).
        """
        top = _torsion(
            bond=(0, 1),
            atoms=[],
            label="the methyl top on C1",
            torsion_id=torsion_handle(Chem.MolFromSmiles(_BUTANE), (0, 1)),
        )
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


class TestTheDihedralIsCheckedToo:
    """The handle guards `bond`; these guard `atoms`, which is what is actually driven.

    Without the check each of these returns a plausible profile with no error.
    """

    def test_a_negative_index_is_refused(self, server: FakeCalcServer) -> None:
        """Python indexes backwards from the end, so this drove a real but different dihedral."""
        with pytest.raises(ValueError, match="not four atoms"):
            _profile(server, bond=_torsion(atoms=[-1, 1, 2, 3]))
        assert server.count("scan_point") == 0

    def test_an_index_past_the_molecule_is_refused_before_the_geometry_arithmetic(
        self, server: FakeCalcServer
    ) -> None:
        """Refused by name, not as a numpy IndexError from the dihedral arithmetic."""
        with pytest.raises(ValueError, match="not four atoms"):
            _profile(server, bond=_torsion(atoms=[0, 1, 2, 99]))

    def test_a_repeated_atom_is_refused(self, server: FakeCalcServer) -> None:
        """Four atoms with one repeated do not define an angle, and returned a barrier anyway."""
        with pytest.raises(ValueError, match="repeats an atom"):
            _profile(server, bond=_torsion(atoms=[0, 1, 2, 2]))

    def test_a_dihedral_that_does_not_turn_about_its_own_bond_is_refused(
        self, server: FakeCalcServer
    ) -> None:
        """The middle pair *is* the bond; anything else profiles a different rotation."""
        with pytest.raises(ValueError, match="does not turn about the bond"):
            _profile(server, bond=_torsion(atoms=[1, 0, 2, 3]))

    def test_a_dihedral_that_is_not_a_bonded_chain_is_refused(self, server: FakeCalcServer) -> None:
        """Four atoms bonded in sequence is what a dihedral means."""
        with pytest.raises(ValueError, match="not a bonded chain"):
            _profile(server, bond=_torsion(atoms=[3, 1, 2, 0]))

    def test_a_step_that_cannot_resolve_the_period_is_refused(self, server: FakeCalcServer) -> None:
        """One or two points over a period makes every well and barrier in it an artefact."""
        with pytest.raises(ValueError, match="cannot resolve"):
            _profile(server, bond=_torsion(period_degrees=20.0), step_degrees=30.0)


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
        """A well is released from its constraint, not reported as its constrained scan point.

        The 45-degree grid does not line up with the wells, so the scan minima (45, 180, 315) differ
        from the released rotamers (60, 180, 300), which is what the test asserts.
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

        Treating it as a line drops the barrier across 0 degrees — for n-butane the syn barrier, the
        highest on the surface.
        """
        profile = _profile(server)
        assert len(profile.barriers) == len(profile.rotamers)
        assert any(
            barrier.at_degrees < 60 or barrier.at_degrees > 300 for barrier in profile.barriers
        )

    def test_a_single_well_per_period_still_has_a_barrier(self, server: FakeCalcServer) -> None:
        """A single well per period still has a barrier.

        An amide or a single-minimum biaryl rotates into its own symmetry image; that pass is the
        barrier VT-NMR measures. Pairing adjacent wells around a ring must not yield a zero-length
        arc when there is only one. Driven over a third of the fake's three-fold potential, which
        holds one well.
        """
        profile = _profile(server, bond=_torsion(symmetry_order=3, period_degrees=120.0))
        assert len(profile.rotamers) == 1
        assert len(profile.barriers) == 1
        barrier = profile.barriers[0]
        assert barrier.from_rotamer == barrier.to_rotamer == 0
        # Symmetry, not coincidence: it is the same well on both sides of the pass.
        assert barrier.forward_kcal == barrier.reverse_kcal
        assert barrier.forward_kcal > 0.0
        assert profile.highest_barrier_kcal == barrier.forward_kcal

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


class TestTheWarnings:
    """A check that fires on the molecules the feature is for is worse than no check."""

    def test_a_steep_real_barrier_is_not_reported_as_a_discontinuity(
        self, server: FakeCalcServer
    ) -> None:
        """A steep real barrier is not reported as a discontinuity.

        A discontinuity is a step out of line with its neighbours, not a large step, so the rule is
        a ratio; an absolute bound would fire on exactly the hindered rotations this capability is
        for. The fake's smooth profile must produce no warning.
        """
        warnings = _profile(server).warnings
        assert not [warning for warning in warnings if "different basin" in warning], warnings

    def test_a_step_far_out_of_line_is_still_reported(self, server: FakeCalcServer) -> None:
        """The check still has to catch what it is for, so the ratio is a threshold, not a mute.

        One point is pushed far off the smooth profile — which is what a relaxation into another
        basin looks like — and the warning must name it.
        """
        smooth = server._optimization
        original = server.overrides.get("scan_point")

        def _one_point_adrift(arguments: dict[str, object]) -> dict[str, object]:
            result = server._scan_point(arguments)
            if float(arguments["value"]) == 90.0:  # type: ignore[arg-type]
                result["energy_hartree"] -= 0.2
            return result

        del smooth, original
        server.overrides["scan_point"] = _one_point_adrift
        warnings = _profile(server).warnings
        assert [warning for warning in warnings if "different basin" in warning], warnings


class TestTheBarrierArithmetic:
    """One energy zero, and a free energy that is a difference."""

    def test_a_barrier_is_measured_from_the_released_well_not_the_scan_point(
        self, server: FakeCalcServer
    ) -> None:
        """A barrier is measured from the released well, not the constrained scan point.

        Barrier above its well plus well above the lowest well is the pass height above the lowest
        released minimum, which must exceed the profile's `relative_kcal` (measured from the lowest
        constrained point) by exactly that well's relaxation. Mixing the two zeros understates
        barriers.
        """
        profile = _profile(server)
        assert profile.barriers, "the premise failed: no barrier to measure"
        above_lowest_well = max(
            barrier.forward_kcal + profile.rotamers[barrier.from_rotamer].relative_kcal
            for barrier in profile.barriers
        )
        above_lowest_point = max(point.relative_kcal for point in profile.points)
        assert above_lowest_well > above_lowest_point, (
            f"the highest pass is {above_lowest_well:.3f} above the lowest released well and "
            f"{above_lowest_point:.3f} above the lowest scan point; releasing lowers a well, so "
            "the first must be the larger — equal means the two zeros are still being mixed"
        )

    def test_a_thorough_barrier_is_a_free_energy_difference_not_an_absolute_correction(
        self, server: FakeCalcServer
    ) -> None:
        """A thorough barrier is `G(pass) - G(well)`, not an absolute `G - E` correction.

        On a surface whose Hessian is the same everywhere the thermal terms cancel, so the answer
        stays close to the electronic barrier.
        """
        electronic = _profile(server)
        free = _profile(server, level="thorough")
        assert [barrier.basis for barrier in free.barriers] == ["G"] * len(free.barriers)
        for barrier in free.barriers:
            assert barrier.forward_kcal < 20.0, (
                f"a {barrier.forward_kcal:.1f} kcal/mol barrier on a surface whose highest pass is "
                f"{electronic.highest_barrier_kcal} — an absolute correction has been added"
            )
        assert free.highest_barrier_kcal is not None
        assert electronic.highest_barrier_kcal is not None
        assert free.highest_barrier_kcal == pytest.approx(electronic.highest_barrier_kcal, abs=1.0)

    def test_a_rotamer_is_the_geometry_its_free_energy_was_computed_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rotamer is the geometry its free energy was computed at.

        Above `quick`, `relax_to_minimum` may escape a saddle and re-optimize; the last Hessian's
        geometry is the one published. `saddle_first` forces that escape on the first well, and the
        test asserts the last Hessian's geometry is one of the published rotamers.
        """
        server = install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 3), saddle_first=True))
        profile = _profile(server, level="standard")
        hessians = server.arguments("compute_hessian")
        assert len(hessians) > len(profile.rotamers), (
            "the premise failed: no well needed a second Hessian, so nothing was refined"
        )
        published = {rotamer.structure_id for rotamer in profile.rotamers}
        last = _structure_id(hessians[-1]["structure"])
        assert last in published, (
            "the last Hessian was taken at a geometry no rotamer reports, so a free energy and a "
            "structure_id in this result describe different structures"
        )


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

        Pinned as literals because `skills/atropisomer-assessment` classifies compounds on these
        numbers.
        """
        assert half_life_from_barrier(barrier, 298.15, 0.0).half_life_seconds == pytest.approx(
            seconds, rel=0.01
        )

    def test_one_kcal_is_about_a_factor_of_five(self) -> None:
        """The reason the band travels with the number rather than being left to a reader."""
        ratio = rate_from_barrier(25.0, 298.15) / rate_from_barrier(26.0, 298.15)
        assert 5.0 == pytest.approx(ratio, rel=0.1)


def _with_dihedral_at(degrees: float) -> dict[str, object]:
    """An n-butane geometry with its central dihedral driven to `degrees`."""
    return with_dihedral(embed(_BUTANE), (0, 1, 2, 3), degrees)


class TestARotorWhoseEndCarriesOnlyHydrogens:
    """`top` and `xh` are two different rotors, and only a `top` is already accounted for.

    `enumerate_torsions` reports both without dihedral atoms. A methyl barrier is inside the
    quasi-RRHO free-rotor treatment; an amide N-H or a carboxylic O-H is not, so an X-H rotor is
    scanned rather than refused with the methyl sentence.
    """

    def test_an_x_h_rotor_is_scanned_in_the_explicit_hydrogen_numbering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ethanol's O-H profile, driven about C0-C1-O2-H8 — a dihedral ending on a hydrogen.

        The numbering is the structure's own explicit-H order (heavy atoms first, hydrogens by
        parent), which `scan_point` validates against `len(structure.elements)`.
        """
        server = install(monkeypatch, FakeCalcServer(torsion=(0, 1, 2, 8)))
        hydroxyl = _torsion(
            "CCO",
            (1, 2),
            atoms=[],
            label="the O-H rotation on C1",
            torsion_id=torsion_handle(Chem.MolFromSmiles("CCO"), (1, 2)),
        )
        profile = asyncio.run(compose.rotation_profile(InMemoryStore(), "CCO", hydroxyl))

        assert profile.atoms == [0, 1, 2, 8], "the dihedral must end on the rotating hydrogen"
        assert server.count("scan_point") > 0
        assert all(call["atoms"] == [0, 1, 2, 8] for call in server.arguments("scan_point"))

    def test_a_dihedral_less_entry_for_a_bond_that_has_one_is_refused_as_malformed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty `atoms` on a bond with a heavy dihedral is malformed.

        n-butane's central bond is neither a top nor an X-H rotor, so it must not be answered with
        the methyl-rotation sentence.
        """
        install(monkeypatch, FakeCalcServer())
        malformed = _torsion(atoms=[])
        with pytest.raises(ValueError, match="enumerate_torsions"):
            asyncio.run(compose.rotation_profile(InMemoryStore(), _BUTANE, malformed))


def test_a_published_profile_names_the_method_the_server_ran(
    server: FakeCalcServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RotationProfile.method` comes off the result, never off local config.

    `publish/project.py` turns it into a published `TheoryLevel`, so a local setting would assert
    the wrong level of theory. The setting is moved so the test fails for the right reason.
    """
    monkeypatch.setattr(settings, "xtb_method", "WRONG-METHOD")

    profile = _profile(server)

    assert profile.method == "GFN2-xTB", "the published record names this pod's config, not the run"
