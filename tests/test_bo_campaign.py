"""Tests for the durable BO campaign (plan step 1d.4).

The registry and activities are exercised directly (fast, no server). The full
durable workflow runs on Temporal's time-skipping server in CI and skips in the
offline sandbox — proving a real reaction campaign runs end-to-end and resumably.
"""

import asyncio
import warnings
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.connectors.bo.activities import evaluate_candidates, propose_initial, propose_next
from chemclaw.connectors.bo.workflows import BoCampaignWorkflow
from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.science.bo.benchmarks.reizman_suzuki import build_problem, load_dataset
from chemclaw.science.bo.campaign import optimize
from chemclaw.science.bo.objectives import (
    MOLECULE_KEY,
    get_objective,
    molecule_library_problem,
    solubility_objective,
)
from chemclaw.science.bo.problem import (
    CampaignCarryOver,
    CampaignResult,
    CampaignSpec,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
    ParamValue,
    best_of,
    discrete_candidate_count,
    distinct_candidate_count,
    pareto_front,
    require_campaign_startable,
    require_rounds_within_ceiling,
)
from chemclaw.science.calc.solubility import SolubilityInput, predict_solubility
from chemclaw.science.calc.store import InMemoryStore
from tests.temporal_env import pydantic_client, start_env_or_skip

warnings.filterwarnings("ignore")

_BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]


@pytest.fixture(autouse=True)
def _no_op_heartbeat_outside_activity_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let the BO activities run directly (not under a Temporal worker) in this file.

    `activity.heartbeat` raises outside a real activity context (Conn-F2 gave all three BO
    activities a heartbeat), and this file calls them directly rather than through Temporal — the
    same idiom `tests/test_calc_jobs.py` uses for the same reason.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)
    yield


def test_get_objective_unknown_raises() -> None:
    """An unknown objective name is a clear error listing the known ones (G4)."""
    with pytest.raises(ValueError, match="unknown objective"):
        get_objective("does-not-exist")


@pytest.mark.parametrize("n_initial", [0, 1])
def test_campaign_spec_rejects_insufficient_seed(n_initial: int) -> None:
    """n_initial below the surrogate floor (2) fails at spec time, not at round 1.

    BoFire's SOBO strategy needs at least two experiments to fit; a spec with
    fewer would burn its seed evaluations and then crash non-retryably.
    """
    problem = build_problem(load_dataset())
    with pytest.raises(ValueError, match="greater than or equal to 2"):
        CampaignSpec(problem=problem, objective_name="reizman_suzuki", n_initial=n_initial)


def test_campaign_spec_carries_per_campaign_seed() -> None:
    """The spec is the per-campaign seed seam; unset means the config default."""
    problem = build_problem(load_dataset())
    spec = CampaignSpec(problem=problem, objective_name="reizman_suzuki")
    assert spec.seed is None  # engine resolves None to settings.bo_seed
    replicate = spec.model_copy(update={"seed": 7})
    assert replicate.seed == 7


def test_best_of_honors_direction() -> None:
    """best_of picks max for maximize and min for minimize."""
    params: list[Parameter] = [ContinuousParameter(name="x", lower=0.0, upper=1.0)]
    observations = [
        Observation(params={"x": 0.0}, value=1.0),
        Observation(params={"x": 1.0}, value=5.0),
    ]
    maximize = OptimizationProblem(
        parameters=params, objectives=[Objective(name="y", direction="maximize")]
    )
    minimize = OptimizationProblem(
        parameters=params, objectives=[Objective(name="y", direction="minimize")]
    )
    assert best_of(maximize, observations).value == 5.0
    assert best_of(minimize, observations).value == 1.0


def test_activities_seed_and_evaluate() -> None:
    """The seed and evaluate activities produce candidates and scored observations."""

    async def _run() -> None:
        problem = build_problem(load_dataset())
        seed = await propose_initial(problem, 3)
        assert len(seed) == 3
        observations = await evaluate_candidates("reizman_suzuki", seed)
        assert len(observations) == 3
        assert all(o.value >= 0 for o in observations)  # yields are non-negative

    asyncio.run(_run())


def test_solubility_objective_scores_via_calculator() -> None:
    """The calculator-backed objective (1d.3) scores a molecule via the cached calculator."""

    async def _run() -> None:
        store = InMemoryStore()
        objective = solubility_objective(store)

        ethanol = await objective({MOLECULE_KEY: "CCO"})
        hexadecane = await objective({MOLECULE_KEY: "CCCCCCCCCCCCCCCC"})

        # The objective returns exactly the calculator's predicted log S...
        assert ethanol == predict_solubility(SolubilityInput(smiles="CCO")).log_s_mol_per_l
        assert ethanol > hexadecane  # ethanol far more soluble than the alkane
        # ...and a repeat is served from the store (same value, no recompute error).
        assert await objective({MOLECULE_KEY: "CCO"}) == ethanol

    asyncio.run(_run())


def test_get_objective_resolves_calculator_objective() -> None:
    """The calculator-backed objective is registered and resolvable by name."""
    assert callable(get_objective("solubility_max"))


def test_candidate_set_bo_finds_soluble_molecule() -> None:
    """Candidate-set BO over a molecule library finds a top molecule sub-exhaustively."""

    async def _run() -> None:
        store = InMemoryStore()
        # 14 diverse molecules; only a few (glycerol, glycol, water, urea) are very soluble.
        library = [
            "CCCCCCCCCCCCCCCC",
            "c1ccccc1",
            "CCCCCCCC",
            "CCCCCCO",
            "CCO",
            "O",
            "OCC(O)CO",
            "NC(=O)N",
            "CC(=O)O",
            "Oc1ccccc1",
            "CCOCC",
            "ClCCl",
            "CCCCCCCCCCCC",
            "OCCO",
        ]
        problem = molecule_library_problem(library)

        result = await optimize(problem, solubility_objective(store), n_initial=4, n_rounds=5)

        all_values = sorted(
            predict_solubility(SolubilityInput(smiles=s)).log_s_mol_per_l for s in library
        )
        median = all_values[len(all_values) // 2]
        assert len(result.history) < len(library)  # BO did not evaluate the whole library
        assert result.best.value > median  # yet steered to a soluble molecule (top half)

    asyncio.run(_run())


def test_discrete_candidate_count() -> None:
    """Pure-categorical spaces are finite (product of categories); mixed spaces are infinite."""
    assert discrete_candidate_count(molecule_library_problem(["CCO", "O", "c1ccccc1"])) == 3
    assert discrete_candidate_count(build_problem(load_dataset())) is None  # has continuous dims


def test_molecule_library_rejects_bad_smiles_up_front() -> None:
    """One unparseable library entry fails at problem construction, naming the entry.

    Without this, the campaign would fail non-retryably only when BO finally
    proposes the bad molecule, discarding all completed rounds.
    """
    with pytest.raises(InvalidSmilesError, match="C1CC"):
        molecule_library_problem(["CCO", "C1CC", "O"])


def test_molecule_library_collapses_duplicate_spellings() -> None:
    """Two spellings of one molecule become one candidate, not two."""
    problem = molecule_library_problem(["CCO", "OCC", "O"])
    parameter = problem.parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    assert parameter.categories == ["CCO", "O"]


def test_optimize_stops_gracefully_on_exhausted_discrete_space() -> None:
    """A budget exceeding the discrete space stops cleanly instead of crashing in BoFire."""

    async def _run() -> None:
        store = InMemoryStore()
        library = ["CCO", "O", "c1ccccc1", "CCCCCCCCCCCCCCCC"]  # only 4 candidates
        problem = molecule_library_problem(library)

        # Budget 2 + 10 far exceeds the 4-candidate space; must not raise.
        result = await optimize(problem, solubility_objective(store), n_initial=2, n_rounds=10)

        best_possible = max(
            predict_solubility(SolubilityInput(smiles=s)).log_s_mol_per_l for s in library
        )
        assert distinct_candidate_count(result.history) <= len(library)
        assert result.best.value == pytest.approx(best_possible)

    asyncio.run(_run())


def test_durable_campaign_runs_end_to_end() -> None:
    """The workflow runs a small Reizman campaign durably and returns a correct result.

    This test's job is the *durable workflow* — that a real campaign seeds, runs its
    rounds, and returns a complete, correctly-reduced result across the Temporal
    serialization boundary. It deliberately does not assert an absolute yield (e.g.
    "beats the dataset median"): a 6-evaluation campaign can't clear that reliably, and
    the BoTorch acqf optimizer's trajectory differs across BLAS/scipy builds, so such a
    threshold is platform-flaky. Optimization *quality* is covered deterministically by
    `test_bo.py`'s convergence tests and `test_candidate_set_bo_finds_soluble_molecule`.
    """

    async def _run() -> None:
        spec = CampaignSpec(
            problem=build_problem(load_dataset()),
            objective_name="reizman_suzuki",
            n_initial=4,
            n_rounds=2,
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-bo",
                workflows=[BoCampaignWorkflow],
                activities=_BO_ACTIVITIES,
            ):
                # The connector contract: payload in, `ConnectorJobResult` out. The campaign's own
                # result travels in `data`, which is why it is re-parsed here rather than typed —
                # core deliberately never knows this shape (D-093).
                envelope = await client.execute_workflow(
                    BoCampaignWorkflow.run,
                    spec.model_dump(mode="json"),
                    id="bo-campaign-test",
                    task_queue="test-bo",
                )
        result = CampaignResult.model_validate(envelope.data)
        # Every round ran and every point was actually evaluated by the objective.
        assert len(result.history) == 6  # 4 seed + 2 rounds x batch 1
        assert all(o.provenance == "predicted" for o in result.history)
        # The best that survived serialization is the true optimum of the returned
        # history — i.e. the durable reduce is correct, not desynced from the history.
        assert result.best == best_of(spec.problem, result.history)
        # And the envelope's summary is the one line the chat shows for a finished campaign.
        assert "reizman_suzuki" in envelope.summary

    asyncio.run(_run())


def test_a_resumed_run_picks_the_campaign_up_instead_of_re_seeding() -> None:
    """The continue-as-new carry-over is a real resumption, not a restart.

    `_carry_on_if_history_is_filling_up` ends a run mid-campaign and hands the next one a
    `CampaignCarryOver`. The trigger is the server's own `is_continue_as_new_suggested()`, which a
    test cannot force without pushing tens of thousands of events through the loop — so what is
    pinned here is the half that carries the risk: that a run *given* a carry-over spends exactly
    the rounds still owed, keeps the observations already paid for, and never re-seeds.

    A re-seed would be the expensive bug — silently paying for `n_initial` evaluations again every
    time the history filled up, on a campaign long enough to fill it more than once.
    """

    async def _run() -> None:
        spec = CampaignSpec(
            problem=build_problem(load_dataset()),
            objective_name="reizman_suzuki",
            n_initial=4,
            n_rounds=5,
        )
        carried = await _seed_history(spec)
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-bo-resume",
                workflows=[BoCampaignWorkflow],
                activities=_BO_ACTIVITIES,
            ):
                envelope = await client.execute_workflow(
                    BoCampaignWorkflow.run,
                    args=[spec.model_dump(mode="json"), carried.model_dump(mode="json")],
                    id="bo-campaign-resume-test",
                    task_queue="test-bo-resume",
                )
        result = CampaignResult.model_validate(envelope.data)
        # Three carried observations plus the two rounds still owed — not 4 seed + 5 rounds, and
        # not 3 + 5: the resumed run honours `rounds_remaining`, not the spec's `n_rounds`.
        assert len(result.history) == 5
        assert result.history[:3] == carried.history
        assert result.best == best_of(spec.problem, result.history)

    asyncio.run(_run())


async def _seed_history(spec: CampaignSpec) -> CampaignCarryOver:
    """Three real observations plus two rounds owed — a campaign caught mid-flight."""
    seed = await propose_initial(spec.problem, 3, spec.seed)
    history = await evaluate_candidates(spec.objective_name, seed)
    return CampaignCarryOver(history=history, rounds_remaining=2)


def test_round_ceiling_is_enforced_at_creation_not_in_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_rounds_within_ceiling` gates creation; the spec model itself stays config-free.

    The ceiling bounds what a spec may *spend* — every round is a real evaluation. It is not what
    keeps the campaign inside Temporal's event history, though it was once described that way;
    `_carry_on_if_history_is_filling_up` does that. But `CampaignSpec` crosses the Temporal
    serialization boundary: a model validator reading live `bo_max_rounds` would make an
    in-flight campaign's own input fail deserialization at replay when the setting is lowered.
    So the ceiling is a creation-time check, and a spec serialized under a higher ceiling must
    still round-trip after the ceiling drops.
    """
    monkeypatch.setattr(settings, "bo_max_rounds", 3)
    with pytest.raises(ValueError, match="bo_max_rounds=3"):
        require_rounds_within_ceiling(4)
    require_rounds_within_ceiling(3)  # at the ceiling is fine — the bound is inclusive

    # Replay survives a lowered ceiling: the in-flight spec's own input still deserializes.
    problem = build_problem(load_dataset())
    spec = CampaignSpec(problem=problem, objective_name="reizman_suzuki", n_rounds=4)
    assert CampaignSpec.model_validate(spec.model_dump()).n_rounds == 4


def test_optimize_rejects_rounds_beyond_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process campaign entry point enforces the config ceiling before spending budget."""
    monkeypatch.setattr(settings, "bo_max_rounds", 3)
    problem = molecule_library_problem(["CCO", "O", "c1ccccc1"])

    async def _never(_params: dict[str, ParamValue]) -> float:
        raise AssertionError("no evaluation may run for a rejected campaign")

    with pytest.raises(ValueError, match="bo_max_rounds=3"):
        asyncio.run(optimize(problem, _never, n_initial=2, n_rounds=4))


# --- multi-objective boundaries (W3) -----------------------------------------------------------


def _two_objective_problem() -> OptimizationProblem:
    """Maximize yield, minimize impurity, over one continuous factor."""
    return OptimizationProblem(
        parameters=[ContinuousParameter(name="t", lower=0.0, upper=100.0)],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )


def _point(t: float, yield_: float, impurity: float) -> Observation:
    """One run reporting both objectives, with `value` mirroring the lead one."""
    return Observation(
        params={"t": t}, value=yield_, values={"yield": yield_, "impurity": impurity}
    )


def test_best_of_refuses_a_trade_off_rather_than_picking_an_axis() -> None:
    """A "best" on a trade-off is the overclaim this whole wave exists to make impossible.

    Silently returning the lead objective's winner would answer "the best conditions" for a
    campaign whose premise is that no such point exists — the same shape as the fabrication the
    old single-objective refusal was written to prevent, arrived at from the other direction.
    """
    with pytest.raises(ValueError, match="pareto_front"):
        best_of(_two_objective_problem(), [_point(10.0, 50.0, 1.0), _point(90.0, 80.0, 5.0)])


def test_the_front_keeps_what_nothing_beats_on_both_axes() -> None:
    """Dominance: at least as good everywhere, strictly better somewhere."""
    problem = _two_objective_problem()
    runs = [
        _point(10.0, 50.0, 1.0),  # best impurity
        _point(50.0, 70.0, 3.0),  # middle, on the front
        _point(90.0, 80.0, 5.0),  # best yield
        _point(30.0, 60.0, 4.0),  # beaten by the middle run on both -> off the front
    ]
    front = pareto_front(problem, runs)
    assert [(o.values["yield"], o.values["impurity"]) for o in front] == [
        (50.0, 1.0),
        (70.0, 3.0),
        (80.0, 5.0),
    ]


def test_a_duplicated_point_stays_on_the_front_twice() -> None:
    """Neither dominates the other, and dropping one would discard a replicate silently."""
    problem = _two_objective_problem()
    runs = [_point(10.0, 50.0, 1.0), _point(10.0, 50.0, 1.0)]
    assert len(pareto_front(problem, runs)) == 2


def test_one_run_dominating_everything_gives_a_front_of_one() -> None:
    """A real and unusual finding, not an error — the skill is told to say so."""
    problem = _two_objective_problem()
    runs = [_point(10.0, 90.0, 0.5), _point(50.0, 70.0, 3.0), _point(90.0, 60.0, 4.0)]
    front = pareto_front(problem, runs)
    assert len(front) == 1
    assert front[0].values == {"yield": 90.0, "impurity": 0.5}


def test_a_single_objective_front_is_just_the_winner() -> None:
    """Dominance collapses to the ordinary comparison when there is one axis."""
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="t", lower=0.0, upper=100.0)],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    runs = [
        Observation(params={"t": 10.0}, value=50.0),
        Observation(params={"t": 50.0}, value=80.0),
        Observation(params={"t": 90.0}, value=70.0),
    ]
    assert [o.value for o in pareto_front(problem, runs)] == [80.0]
    assert best_of(problem, runs).value == 80.0


def test_the_durable_campaign_refuses_a_multi_objective_spec() -> None:
    """The registry maps a name to one scalar-returning callable, so a trade-off has no evaluator.

    Refused at launch with a message naming the inline tool, rather than optimizing the lead
    objective for ten rounds and reporting a "best" nobody asked for.
    """
    spec = CampaignSpec(
        problem=_two_objective_problem(), objective_name="reizman_suzuki", n_rounds=3
    )
    with pytest.raises(ValueError, match="suggest_next_experiment"):
        require_campaign_startable(spec)


def test_the_precondition_still_enforces_the_round_ceiling() -> None:
    """Folding two rules into one function must not drop the one that was already there."""
    spec = CampaignSpec(
        problem=OptimizationProblem(
            parameters=[ContinuousParameter(name="t", lower=0.0, upper=1.0)],
            objectives=[Objective(name="yield", direction="maximize")],
        ),
        objective_name="reizman_suzuki",
        n_rounds=settings.bo_max_rounds + 1,
    )
    with pytest.raises(ValueError, match="bo_max_rounds"):
        require_campaign_startable(spec)


def test_the_manifest_names_a_precondition_that_accepts_the_params_model() -> None:
    """`connector-validate` checks this, and an earlier manifest named a function taking an int.

    Every `start_optimization_campaign` call then raised `TypeError` while CI stayed green, so the
    reference connector's flagship job could not be started at all. Pinned here too, because this
    wave renamed the function the manifest points at.
    """
    from chemclaw.connectors.jobs import resolve_precondition
    from chemclaw.connectors.registry import discovered

    _, manifest = discovered()["bo"]
    job = next(job for job in manifest.jobs if job.name == "start_optimization_campaign")
    assert job.precondition is not None
    resolve_precondition(job.precondition)(
        CampaignSpec(
            problem=OptimizationProblem(
                parameters=[ContinuousParameter(name="t", lower=0.0, upper=1.0)],
                objectives=[Objective(name="yield", direction="maximize")],
            ),
            objective_name="reizman_suzuki",
            n_rounds=2,
        )
    )


def test_optimize_refuses_a_trade_off_before_spending_any_budget() -> None:
    """It returns one best observation, so a trade-off has no answer for it.

    The refusal used to come from `best_of` *after* the loop, i.e. after every evaluation had been
    paid for. The docstring already promised the round ceiling was "rejected here, before any
    budget is spent"; this holds the same promise for the objective count.
    """
    problem = OptimizationProblem(
        parameters=[ContinuousParameter(name="temperature", lower=20.0, upper=120.0)],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )
    calls = 0

    async def _evaluate(params: dict[str, ParamValue]) -> float:
        nonlocal calls
        calls += 1
        return 1.0

    with pytest.raises(ValueError, match="no single best point"):
        asyncio.run(optimize(problem, _evaluate, n_initial=2, n_rounds=1))
    assert calls == 0
