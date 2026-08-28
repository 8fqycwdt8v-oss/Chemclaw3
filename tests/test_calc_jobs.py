"""The `calc` bundle's durable job activity threads its request through to the composition (D-118).

`run_xtb_calculation` is the only caller of the reaction and solvent-screen composites on the
durable path, and it dispatches on an `XtbJobSpec` member whose fields it copies across by hand. So
a field that exists in the composite's signature and not in the spec — or exists in both and is not
copied — is invisible: the job runs, returns a well-formed result, and quietly answers a smaller
question than the one that was asked.

That is exactly what happened to `symmetry_numbers`. The composite grew it as the input a free
energy is not computed without, `ReactionJobSpec`/`SolventScreenJobSpec` did not, and both call
sites were positional up to `level` — so nothing broke and the durable `compute_reaction_energy` and
`compare_solvents` jobs simply stopped reporting a free energy at all. A chemist who ran the job
instead of the inline tool got ΔE, and no indication that the ΔG they asked for had been withheld
for a reason they could have fixed.

**The physics is a fake and that is now the honest choice**, where before this file ran real GFN2 on
diatomics. The calculations left this repository
(`D-2026-08-16-the-physics-leaves-the-cache-stays`); what the activity is responsible for is the
passthrough, the summary line and the heartbeating, and every one of those is visible against
`tests/calc_server_fake.py`. What a fake cannot check — that the numbers are chemistry — is checked
in the repository that computes them.
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
    ComplexJobSpec,
    EnsembleJobSpec,
    MicrostatePkaJobSpec,
    ReactionJobSpec,
    RotationJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    TorsionSpec,
    XtbJobSpec,
)
from chemclaw.connectors.calc.workflows import CalcJobWorkflow
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    turn_headers,
)
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.chem import torsion_handle
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install
from tests.temporal_env import pydantic_client, start_env_or_skip

# H2 + Cl2 -> 2 HCl. Every species is a closed-shell diatomic, and the shape that matters: the two
# homonuclear reactants are D∞h (sigma=2) and the product is C∞v (sigma=1), so sigma does *not*
# cancel across the arrow and the map has to reach the calculation to change anything. The three
# distinct values also pin the map key by key rather than as a whole.
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

    `activity.heartbeat` raises outside an activity context, and it is passed down as the progress
    callback *and* used by the shared heartbeat timer, so this is what makes the real function
    callable at all from a test.
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

    `symmetry_number` per species is asserted alongside ΔG because it pins the map *key by key*: a
    passthrough that dropped the values and passed an empty map would still yield None, but one that
    mismatched Cl2's 2 onto HCl would not be visible in ΔG alone.
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
    """HCl appears twice in the equation and is one calculation, which is the cache doing its job.

    Four stoichiometric entries, three distinct species. The reaction is a subtraction over
    per-species entries that are keyed individually — which is why there is deliberately no
    reaction-level cache row.
    """
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

    Atoms, values and solvent all have to reach the composition, or the profile is a different
    question answered confidently. The summary names the coordinate the result reports, not the one
    the request asked for.
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

    The summary is what a completion push-back and a job listing show, so it has to carry the three
    things a chemist would otherwise have to open the payload for: which bond, how high, and how
    long that holds — the last one **as a range**, because a single half-life from a semiempirical
    barrier reads exactly like a measurement.
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

    The site is perceived from the winning geometry on the server side, so this asserts the
    passthrough rather than the perception: what a chemist reads in the job list has to name the
    equilibrium that was computed, because "pKa 9.9" for a molecule with three ionisable centres is
    an answer to a question nobody asked.
    """
    result = _run(MicrostatePkaJobSpec(smiles="Oc1ccccc1"))

    assert result.pka is not None
    assert result.pka.branch == "acid"
    assert result.pka.site_smiles == "[O-]c1ccccc1"
    assert server.count("search_conformer_ensemble") == 2
    assert "[O-]c1ccccc1" in result.summary


def test_a_pka_job_carries_the_branch_into_its_summary(server: FakeCalcServer) -> None:
    """A base reports `pKaH`, not `pKa`, and the summary is where a reader sees which.

    They are different numbers about different equilibria — pyridine's 5.2 is its conjugate acid's —
    and a job list that called both "pKa" would invite exactly the confusion the branch field exists
    to prevent.
    """
    result = _run(MicrostatePkaJobSpec(smiles="c1ccncc1"))

    assert result.pka is not None and result.pka.branch == "base"
    assert "pKaH" in result.summary


def test_the_remote_call_names_the_person_the_durable_run_is_for(
    monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer
) -> None:
    """Every request to the calculation server used to be anonymous on the durable path.

    The activity's outbound calls carry `connectors.identity.turn_headers()`, which reads the
    ambient identity — and nothing on this path bound one, so the heaviest server in the fleet
    (minutes-to-hours CREST runs) logged `actor=- session=-` for every job while the same tool
    called inline from a chat turn was fully attributed. Recorded at the boundary the real code
    calls rather than inside it: the header builder is asked, at the moment of the remote call,
    exactly what it would put on the wire.
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
    """A worker runs the next job in the same process, so a leaked stamp becomes a false one."""
    asyncio.run(activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO"), "chemist-1", "job-1"))
    assert HEADER_ACTOR not in turn_headers()
    assert HEADER_CORRELATION not in turn_headers()


def test_a_run_with_no_memo_stamps_nothing_rather_than_a_placeholder(
    monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer
) -> None:
    """Absent identity stays absent: an empty header would let a log claim an anonymous caller.

    The defaults also keep a run started before these arguments existed decodable, which is why
    they are empty strings rather than a required field.
    """
    seen: list[dict[str, str]] = []
    answer = server.call_tool

    async def _record(name: str, arguments: dict[str, object]) -> object:
        seen.append(turn_headers())
        return await answer(name, arguments)

    monkeypatch.setattr(server, "call_tool", _record)
    asyncio.run(activities.run_xtb_calculation(EnsembleJobSpec(smiles="CCO")))
    assert seen and all(HEADER_ACTOR not in headers for headers in seen), seen


def test_the_workflow_hands_the_activity_the_actor_off_the_runs_memo(
    server: FakeCalcServer,
) -> None:
    """The other half of the same route: identity has to reach the activity to be stampable.

    `ConnectorJobWorkflow` puts `requested_by` and `correlation_id` on the child's **memo** —
    deliberately not in the payload, which is model-authored and is the cache key — and this
    bundle's workflow never read them, so the activity had nothing to stamp however carefully it
    stamped it. Driven on the real server against a stand-in activity registered under the
    production name, because what is under test is the argument the workflow sends rather than the
    calculation; the stand-in answers with a result the *real* activity produced against the fake
    server, so the workflow's own `job_envelope` still runs on a shape it would really see.
    """
    seen: list[tuple[str, str]] = []
    answer = _run(EnsembleJobSpec(smiles="CCO"))

    @activity.defn(name="run_xtb_calculation")
    async def _capture(spec: XtbJobSpec, actor: str = "", correlation_id: str = "") -> XtbJobResult:
        """Stand in for the real activity and record the identity it was handed."""
        seen.append((actor, correlation_id))
        return answer

    async def _run_workflow() -> None:
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

    asyncio.run(_run_workflow())
    assert seen == [("chemist-1", "job-correlation-1")]
