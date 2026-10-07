"""The `calc` bundle's durable job activity threads its request through to the composition (D-118).

`run_xtb_calculation` copies `XtbJobSpec` fields into the composites by hand, so a field missing
from the spec or not copied silently answers a smaller question — e.g. `symmetry_numbers`, without
which no free energy is reported. The physics is `tests/calc_server_fake.py`: the activity owns
the passthrough, the summary and the heartbeating.
"""

import asyncio
from collections.abc import Iterator

import pytest
from rdkit import Chem
from temporalio import activity
from temporalio.worker import Worker

from chemclaw.connectors.calc import activities
from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.specs import (
    BondCleavageSpec,
    BondSurveyJobSpec,
    ComplexJobSpec,
    EnsembleJobSpec,
    MicrostatePkaJobSpec,
    ReactionJobSpec,
    RotationJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    SpeciesSolventScreenJobSpec,
    TorsionSpec,
    XtbJobSpec,
)
from chemclaw.connectors.calc.workflows import CalcJobWorkflow
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.call_identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    turn_headers,
)
from chemclaw.core.chem import torsion_handle
from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install
from tests.temporal_env import pydantic_client, start_env_or_skip

# H2 + Cl2 -> 2 HCl: the reactants are D∞h (sigma=2) and the product C∞v (sigma=1), so sigma does
# not cancel and the map must reach the calculation; three distinct values pin it key by key.
_REACTANTS = ["[H][H]", "ClCl"]
_PRODUCTS = ["Cl", "Cl"]
_SIGMAS = {"[H][H]": 2, "ClCl": 2, "Cl": 1}


@pytest.fixture
def store() -> InMemoryStore:
    """One store per test, so a job's own species-sharing is what the cache counts show."""
    return InMemoryStore()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, store: InMemoryStore) -> Iterator[FakeCalcServer]:
    """Run the activity outside Temporal: its own store, a fake server, a heartbeat going nowhere.

    `activity.heartbeat` raises outside an activity context and is used as the progress callback and
    by the heartbeat timer.
    """
    yield _outside_temporal(monkeypatch, store, FakeCalcServer())


@pytest.fixture
def rotation_server(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryStore
) -> Iterator[FakeCalcServer]:
    """The same, over a server carrying a torsional potential — so a profile has wells to find."""
    yield _outside_temporal(monkeypatch, store, FakeCalcServer(torsion=(0, 1, 2, 3)))


def _outside_temporal(
    monkeypatch: pytest.MonkeyPatch, store: InMemoryStore, server: FakeCalcServer
) -> FakeCalcServer:
    """The three patches that make a durable activity callable from a test, written once."""
    monkeypatch.setattr(activities, "default_store", lambda: store)
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)
    return install(monkeypatch, server)


def _run(spec: XtbJobSpec) -> XtbJobResult:
    """Run one durable xTB job to completion, as its worker would."""
    return asyncio.run(activities.run_xtb_calculation(spec))


def test_a_reaction_job_that_states_its_symmetry_numbers_gets_a_free_energy(
    server: FakeCalcServer,
) -> None:
    """The passthrough, proven by the number that only exists when it works.

    Per-species `symmetry_number` pins the map key by key, which ΔG alone would not.
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


def test_a_reaction_job_without_symmetry_numbers_withholds_the_free_energy(
    server: FakeCalcServer,
) -> None:
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


def test_the_repeated_product_is_computed_once(server: FakeCalcServer) -> None:
    """HCl appears twice in the equation and is one calculation: the cache doing its job."""
    result = _run(
        ReactionJobSpec(reactants=_REACTANTS, products=_PRODUCTS, symmetry_numbers=_SIGMAS)
    )
    assert result.reaction is not None
    assert len(result.reaction.species) == 4
    assert server.count("relax_structure") == 3
    assert server.count("compute_hessian") == 3


def test_a_solvent_screen_threads_the_same_map_through_every_solvent(
    server: FakeCalcServer,
) -> None:
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


def test_a_scan_job_threads_its_coordinate_and_summarizes_the_profile(
    server: FakeCalcServer,
) -> None:
    """A scan job threads its coordinate through and summarizes the profile it got back.

    The summary names the coordinate the result reports.
    """
    result = _run(
        ScanJobSpec(smiles="CCCC", atoms=[0, 1, 2, 3], values=[0.0, 60.0, 120.0], solvent="water")
    )
    assert result.scan is not None
    assert result.scan.coordinate == "dihedral"
    assert [point.value for point in result.scan.points] == [0.0, 60.0, 120.0]
    assert server.count("scan_point") == 3
    assert all(args["solvent"] == "water" for args in server.arguments("scan_point"))
    assert "dihedral scan of CCCC" in result.summary


def test_an_ensemble_job_reports_the_populations_it_weighted(server: FakeCalcServer) -> None:
    """One search, weighted here — the summary quotes the lowest member's population.

    The search is the cached half and the weighting is not, because populations depend on a
    temperature the search never saw.
    """
    result = _run(EnsembleJobSpec(smiles="CCCC", search="conformers", effort="quick"))
    assert result.ensemble is not None
    assert result.ensemble.total_found == 3
    assert server.count("search_conformer_ensemble") == 1
    assert "conformers of CCCC: 3 found" in result.summary


def test_a_complex_job_names_the_pair_the_calculation_actually_ran_on(
    server: FakeCalcServer,
) -> None:
    """The summary names the pair the calculation actually ran on.

    Named from the result, not the request: the pair is canonically ordered so that either direction
    is one cache entry, and the summary should describe what ran.
    """
    result = _run(ComplexJobSpec(smiles_a="CO", smiles_b="O"))
    assert result.interaction is not None
    assert (result.interaction.smiles_a, result.interaction.smiles_b) == ("CO", "O")
    assert result.summary.startswith("CO + O:")
    assert server.count("search_binding_modes") == 1


def test_a_rotation_job_names_the_bond_it_profiled_and_times_the_barrier(
    rotation_server: FakeCalcServer,
) -> None:
    """The durable path end to end: spec in, envelope out, with the barrier as a lifetime.

    The summary carries which bond, how high, and how long that holds — as a range, since a single
    half-life from a semiempirical barrier reads like a measurement.
    """
    torsion = TorsionSpec(
        torsion_id=torsion_handle(Chem.MolFromSmiles("CCCC"), (1, 2)),
        atoms=[0, 1, 2, 3],
        bond=[1, 2],
        label="the C1-C2 bond",
    )
    result = _run(RotationJobSpec(smiles="CCCC", torsion=torsion, solvent="water"))
    assert result.rotation is not None
    assert result.rotation.label == "the C1-C2 bond"
    assert result.rotation.torsion_id == torsion.torsion_id
    assert len(result.rotation.rotamers) == 3
    assert all(args["solvent"] == "water" for args in rotation_server.arguments("scan_point"))
    assert "the C1-C2 bond" in result.summary
    assert "t1/2" in result.summary and " to " in result.summary


def test_a_rotation_job_refuses_a_handle_that_is_not_this_molecule_s(
    rotation_server: FakeCalcServer,
) -> None:
    """A wrong bond must be an error on the durable path too, not a profile of something else."""
    torsion = TorsionSpec(
        torsion_id=torsion_handle(Chem.MolFromSmiles("CCCCC"), (1, 2)),
        atoms=[0, 1, 2, 3],
        bond=[1, 2],
        label="a bond of a different molecule",
    )
    with pytest.raises(ValueError, match="does not name a bond of"):
        _run(RotationJobSpec(smiles="CCCC", torsion=torsion))
    assert rotation_server.count("scan_point") == 0


def test_a_pka_job_names_the_proton_it_is_about(server: FakeCalcServer) -> None:
    """Two searches, and a summary that says *which* proton — the half a bare pKa does not carry.

    The site is perceived server-side; this asserts the passthrough.
    """
    result = _run(MicrostatePkaJobSpec(smiles="Oc1ccccc1"))

    assert result.pka is not None
    assert result.pka.branch == "acid"
    assert result.pka.site_smiles == "[O-]c1ccccc1"
    assert server.count("search_conformer_ensemble") == 2
    assert "[O-]c1ccccc1" in result.summary


def test_a_survey_that_lost_a_bond_says_so_in_its_summary(server: FakeCalcServer) -> None:
    """The summary is the line people read, so a survey that lost a bond cannot hide it there."""

    def refusing(arguments: dict[str, object]) -> dict[str, object]:
        if arguments["smiles"] == "[CH3]":
            raise ValueError("no parameters for this input on the server")
        return server._embed_structure(arguments)

    server.overrides["embed_structure"] = refusing
    result = _run(
        BondSurveyJobSpec(
            smiles="CCc1ccccc1",
            cleavages=[
                BondCleavageSpec(atoms=[0, 1], bond="C-C", fragments=["[CH2]c1ccccc1", "[CH3]"]),
                BondCleavageSpec(atoms=[1, 2], bond="C-C", fragments=["[CH2]C", "[c]1ccccc1"]),
            ],
        )
    )

    assert result.bonds is not None and len(result.bonds.failed) == 1
    assert "weakest of 1 bonds" in result.summary
    assert "1 of the bonds could not be computed" in result.summary


def test_a_screen_left_with_one_medium_does_not_summarise_a_comparison(
    server: FakeCalcServer,
) -> None:
    """A spread over one surviving medium is a finding about a comparison that never happened."""

    def refusing(arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("solvent") == "water":
            raise ValueError("no parameters for this input on the server")
        return server._relax_structure(arguments)

    server.overrides["relax_structure"] = refusing
    result = _run(
        SolventScreenJobSpec(
            reactants=["CC(=O)O", "CCO"],
            products=["CC(=O)OCC", "O"],
            solvents=["water"],
            symmetry_numbers={"CC(=O)O": 1, "CCO": 1, "CC(=O)OCC": 1, "O": 2},
        )
    )

    assert "nothing was compared" in result.summary
    assert "spread" not in result.summary
    assert "1 of the media could not be computed" in result.summary


def test_a_species_screen_that_lost_a_medium_says_so_in_its_summary(
    server: FakeCalcServer,
) -> None:
    """The count of media not ranked is in the line people read, not only in the payload."""

    def refusing(arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("solvent") == "toluene":
            raise ValueError("no parameters for this input on the server")
        return server._relax_structure(arguments)

    server.overrides["relax_structure"] = refusing
    result = _run(
        SpeciesSolventScreenJobSpec(
            species=["CC(=O)CC(C)=O", "CC(O)=CC(C)=O"],
            labels=["keto", "enol"],
            solvents=["water", "toluene"],
            ranking="tautomers",
        )
    )

    assert "across 2 media" in result.summary
    assert "1 of the media could not be computed" in result.summary


def test_a_pka_job_carries_the_branch_into_its_summary(server: FakeCalcServer) -> None:
    """A base reports `pKaH`, not `pKa`, and the summary is where a reader sees which."""
    result = _run(MicrostatePkaJobSpec(smiles="c1ccncc1"))

    assert result.pka is not None and result.pka.branch == "base"
    assert "pKaH" in result.summary


def test_the_remote_call_names_the_person_the_durable_run_is_for(
    monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer
) -> None:
    """The remote call names the person the durable run is for.

    Outbound calls carry `connectors.identity.turn_headers()`, so the activity binds the run's
    identity. Recorded at the header builder at the moment of the remote call.
    """
    seen: list[dict[str, str]] = []
    answer = server.call_tool

    async def _record(name: str, arguments: dict[str, object]) -> object:
        seen.append(turn_headers())
        return await answer(name, arguments)

    monkeypatch.setattr(server, "call_tool", _record)

    asyncio.run(
        activities.run_xtb_calculation(
            EnsembleJobSpec(smiles="CCO"), "chemist-1", "job-correlation-1"
        )
    )

    assert seen, "the job made no remote call at all, so this proves nothing"
    assert all(headers.get(HEADER_ACTOR) == "chemist-1" for headers in seen), seen
    assert all(headers.get(HEADER_CORRELATION) == "job-correlation-1" for headers in seen), seen


def test_the_identity_is_unstamped_when_the_job_ends(
    monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer
) -> None:
    """The stamp is removed when the dispatch ends, on the returning path and the raising one.

    Read in the same task that set it: `asyncio.run` runs in a copied context, where the assertion
    could never see the contextvar.
    """

    async def _returned() -> dict[str, str]:
        await activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO"), "chemist-1", "job-1")
        return turn_headers()

    returned = asyncio.run(_returned())
    assert HEADER_ACTOR not in returned and HEADER_CORRELATION not in returned, returned

    async def _raised() -> dict[str, str]:
        async def _refuse(name: str, arguments: dict[str, object]) -> object:
            raise RuntimeError("the backend refused this calculation")

        monkeypatch.setattr(server, "call_tool", _refuse)
        with pytest.raises(Exception, match="stopped answering"):
            await activities.run_xtb_calculation(
                EnsembleJobSpec(smiles="CCO"), "chemist-1", "job-1"
            )
        return turn_headers()

    raised = asyncio.run(_raised())
    assert HEADER_ACTOR not in raised and HEADER_CORRELATION not in raised, raised


def test_a_direct_call_with_no_identity_stamps_nothing_rather_than_a_placeholder(
    monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer
) -> None:
    """Absent identity stays absent: an empty header would let a log claim an anonymous caller.

    The defaults are empty strings so older runs still decode. This covers a direct call; a durable
    run with no memo is the next test.
    """
    seen: list[dict[str, str]] = []
    answer = server.call_tool

    async def _record(name: str, arguments: dict[str, object]) -> object:
        seen.append(turn_headers())
        return await answer(name, arguments)

    monkeypatch.setattr(server, "call_tool", _record)
    asyncio.run(activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO")))
    assert seen and all(HEADER_ACTOR not in headers for headers in seen), seen


async def test_the_workflow_hands_the_activity_the_actor_off_the_runs_memo(
    server: FakeCalcServer,
) -> None:
    """The workflow hands the activity the actor off the run's memo.

    `ConnectorJobWorkflow` puts `requested_by` and `correlation_id` on the child's memo, not the
    model-authored payload. Driven on the real server with a stand-in activity under the production
    name, answering with a result the real activity produced so `job_envelope` sees a real shape.
    """
    seen: list[tuple[str, str]] = []
    # Awaited rather than routed through `_run`, which owns an `asyncio.run` of its own:
    # this test is already inside a loop, and a nested `asyncio.run` refuses outright.
    answer = await activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO"))

    @activity.defn(name="run_xtb_calculation")
    async def _capture(spec: XtbJobSpec, actor: str = "", correlation_id: str = "") -> XtbJobResult:
        """Stand in for the real activity and record the identity it was handed."""
        seen.append((actor, correlation_id))
        return answer

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        queue = bundle_queue("calc")
        async with Worker(
            client, task_queue=queue, workflows=[CalcJobWorkflow], activities=[_capture]
        ):
            await client.execute_workflow(
                CalcJobWorkflow.run,
                EnsembleJobSpec(smiles="CCO"),
                id="calc-memo-identity",
                task_queue=queue,
                memo={"requested_by": "chemist-1", "correlation_id": "job-correlation-1"},
            )

    assert seen == [("chemist-1", "job-correlation-1")]


async def test_a_durable_run_with_no_memo_is_attributed_to_the_service_identity(
    server: FakeCalcServer,
) -> None:
    """A durable run with no memo is attributed to the service identity.

    `CalcJobWorkflow` defaults the memo read to `settings.service_actor_id`, as `connectors/bo`
    does, so the wire carries `X-Chemclaw-Actor: service-account`. Unreachable today (the memo is
    always set); pinned as a characterisation of the fallback an operator would see.
    """
    seen: list[tuple[str, str]] = []
    # Awaited rather than routed through `_run`, which owns an `asyncio.run` of its own:
    # this test is already inside a loop, and a nested `asyncio.run` refuses outright.
    answer = await activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO"))

    @activity.defn(name="run_xtb_calculation")
    async def _capture(spec: XtbJobSpec, actor: str = "", correlation_id: str = "") -> XtbJobResult:
        """Stand in for the real activity and record the identity it was handed."""
        seen.append((actor, correlation_id))
        return answer

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        queue = bundle_queue("calc")
        async with Worker(
            client, task_queue=queue, workflows=[CalcJobWorkflow], activities=[_capture]
        ):
            await client.execute_workflow(
                CalcJobWorkflow.run,
                EnsembleJobSpec(smiles="CCO"),
                id="calc-no-memo-identity",
                task_queue=queue,
            )

    assert seen == [(settings.service_actor_id, "")]


def test_a_summary_names_the_items_the_clock_stopped_apart_from_the_refused_ones() -> None:
    """The remedy differs (a smaller job or a larger budget), so the line people read says which."""
    from chemclaw.connectors.calc.activities import _not_computed
    from chemclaw.science.calc.models import FailedMedium

    failed = [
        FailedMedium(solvent="dmso", reason="stopped", cause="time_budget"),
        FailedMedium(solvent="water", reason="refused"),
    ]
    assert _not_computed(failed, "media") == (
        "; 2 of the media could not be computed, 1 stopped by the time budget (see failed)"
    )
    refused_only = "; 1 of the media could not be computed (see failed)"
    assert _not_computed(failed[1:], "media") == refused_only
    assert _not_computed([], "media") == ""
