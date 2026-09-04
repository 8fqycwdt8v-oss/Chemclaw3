"""Tests for the durable BO campaign (plan step 1d.4).

The registry and activities are exercised directly (fast, no server). The full
durable workflow runs on Temporal's time-skipping server in CI and skips in the
offline sandbox — proving a real reaction campaign runs end-to-end and resumably.
"""

import asyncio
import warnings
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest
from rdkit import Chem
from rdkit.Chem import Crippen
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from chemclaw.connectors.bo.activities import evaluate_candidates, propose_initial
from chemclaw.connectors.bo.workflows import BoCampaignWorkflow
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.durable.awaiting import AwaitAnswerWorkflow
from chemclaw.durable.connector_job import child_execution_timeout
from chemclaw.durable.registry import registered_activities
from chemclaw.science.bo.benchmarks.reizman_suzuki import build_problem, load_dataset
from chemclaw.science.bo.campaign import optimize
from chemclaw.science.bo.campaign_record import (
    InMemoryCampaignStore,
    campaign_id_for,
    campaign_store,
)
from chemclaw.science.bo.objectives import (
    MEASURED_OBJECTIVE,
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
from tests.temporal_env import (
    pydantic_client,
    start_env_or_skip,
    start_local_env_or_skip,
)

warnings.filterwarnings("ignore")

# Taken from the registry rather than written out, for the reason the registry exists: a
# hand-maintained list re-creates the "written, imported, absent from the worker's list, never
# runs" failure one level down, and the durable campaign's record-writing activity was added to
# this workflow long after this list was first spelled (`chemclaw.durable.registry`).
_BO_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities(bundle_queue("bo"))


@pytest.fixture(autouse=True)
def _no_op_heartbeat_outside_activity_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let the BO activities run directly (not under a Temporal worker) in this file.

    `activity.heartbeat` raises outside a real activity context (Conn-F2 gave all three BO
    activities a heartbeat), and this file calls them directly rather than through Temporal — the
    same idiom `tests/test_calc_jobs.py` uses for the same reason.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)
    yield


# A deterministic stand-in for the solubility calculator. The model itself is the calculation
# server's since `D-2026-08-16-the-physics-leaves-the-cache-stays`, and `solubility_objective` now
# takes the scorer as an argument for exactly that reason (`science/bo/objectives.py::LogSFor`) —
# so what these tests inject is a monotone surrogate rather than a mock of a network call. It is
# the honest shape for them: what is under test is the *search*, and a search is tested by whether
# it finds the best member of a library under an objective it does not get to see.
def _log_s(smiles: str) -> float:
    """Negated Crippen LogP — ordered like an aqueous solubility and free of any server."""
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    return -float(Crippen.MolLogP(molecule))


async def _log_s_for(smiles: str) -> float:
    """`_log_s` in the shape `solubility_objective` consumes."""
    return _log_s(smiles)


def test_get_objective_unknown_raises() -> None:
    """An unknown objective name is a clear error listing the known ones (G4)."""
    with pytest.raises(ValueError, match="unknown objective"):
        get_objective("does-not-exist", _log_s_for)


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
        objective = solubility_objective(_log_s_for)

        ethanol = await objective({MOLECULE_KEY: "CCO"})
        hexadecane = await objective({MOLECULE_KEY: "CCCCCCCCCCCCCCCC"})

        # The objective returns exactly the calculator's predicted log S...
        assert ethanol == _log_s("CCO")
        assert ethanol > hexadecane  # ethanol far more soluble than the alkane
        # ...and a repeat is served from the store (same value, no recompute error).
        assert await objective({MOLECULE_KEY: "CCO"}) == ethanol

    asyncio.run(_run())


def test_get_objective_resolves_calculator_objective() -> None:
    """The calculator-backed objective is registered and resolvable by name."""
    assert callable(get_objective("solubility_max", _log_s_for))


def test_candidate_set_bo_finds_soluble_molecule() -> None:
    """Candidate-set BO over a molecule library finds a top molecule sub-exhaustively."""

    async def _run() -> None:
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

        result = await optimize(problem, solubility_objective(_log_s_for), n_initial=4, n_rounds=5)

        all_values = sorted(_log_s(s) for s in library)
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
        library = ["CCO", "O", "c1ccccc1", "CCCCCCCCCCCCCCCC"]  # only 4 candidates
        problem = molecule_library_problem(library)

        # Budget 2 + 10 far exceeds the 4-candidate space; must not raise.
        result = await optimize(problem, solubility_objective(_log_s_for), n_initial=2, n_rounds=10)

        best_possible = max(_log_s(s) for s in library)
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


def test_the_evaluation_budget_is_bounded_and_not_only_the_round_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round is not a unit of cost, and the round ceiling was read as though it were.

    `require_rounds_within_ceiling` says, correctly, that "every round costs a real evaluation" —
    but a round costs `batch` of them, and `batch` has no upper bound. So a spec sitting *inside*
    the round ceiling could ask for arbitrarily many evaluations, each one a registered objective
    that may call an uncached calculator. The ceiling written to refuse "a spec that would spend
    thousands of evaluations" permitted exactly that, because it never multiplied.

    Pinned here at both ends: the round count alone passes, and the same spec is refused once the
    batch is what makes it expensive.
    """
    monkeypatch.setattr(settings, "bo_max_rounds", 500)
    monkeypatch.setattr(settings, "bo_max_evaluations", 100)
    problem = build_problem(load_dataset())

    # Inside the round ceiling — 10 rounds of 50 is 505 evaluations, five times the budget.
    with pytest.raises(ValueError, match="objective evaluation"):
        require_campaign_startable(
            CampaignSpec(
                problem=problem,
                objective_name="reizman_suzuki",
                n_initial=5,
                n_rounds=10,
                batch=50,
            )
        )
    # The same round count at batch 1 is 15 evaluations and is fine, which is what makes the
    # assertion above about the budget rather than about the rounds.
    require_campaign_startable(
        CampaignSpec(
            problem=problem, objective_name="reizman_suzuki", n_initial=5, n_rounds=10, batch=1
        )
    )


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


def test_a_campaign_declaring_the_wrong_direction_is_refused_at_launch() -> None:
    """The inverted campaign: every number right, the recommendation exactly backwards.

    A campaign carries its direction twice — in `CampaignSpec.problem.objectives[0].direction`,
    which is what BoFire optimizes, and implicitly in what the *registered* objective means — and
    nothing compared them. So `objective_name="solubility_max"` with `direction="minimize"` ran to
    completion, spent the full evaluation budget, wrote a PR-gated `bo-candidate` note, and
    recommended the **least** soluble molecule in the library as its best point.

    That is the failure a reviewer is least able to catch: nothing in the note is false. The
    conditions were really evaluated, the objective value really is what the model computed, and
    the campaign really did find the extremum it was asked for. Only the direction was wrong, and
    the note does not carry the registry's opinion of which way is better.

    Refused at launch, before an evaluation budget is spent, and the message names the fix.
    """
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="molecule", categories=["CCO", "CCCO"])],
        objectives=[Objective(name="log_s", direction="minimize")],
    )
    spec = CampaignSpec(problem=problem, objective_name="solubility_max", n_rounds=2)

    with pytest.raises(ValueError, match="backwards"):
        require_campaign_startable(spec)


def test_the_direction_rule_costs_nothing_and_passes_the_agreeing_case() -> None:
    """The other half, and the reason `registered_direction` is not `get_objective`.

    A campaign whose declared direction agrees with the registry must start — an over-eager rule
    here would refuse every real campaign, which is the failure mode a one-sided test misses.

    And it is answered *without building the objective*: `solubility_objective` closes over a
    calculator client this precondition has no business constructing, and `_reizman_suzuki` fits a
    surrogate from a bundled dataset. A campaign refused — or accepted — for its direction should
    cost neither. `registered_direction` reads the registry row and stops there, which is what makes
    this test run in milliseconds rather than fitting a model.
    """
    problem = OptimizationProblem(
        parameters=[CategoricalParameter(name="molecule", categories=["CCO", "CCCO"])],
        objectives=[Objective(name="log_s", direction="maximize")],
    )
    require_campaign_startable(CampaignSpec(problem=problem, objective_name="solubility_max"))


def test_every_registered_objective_declares_a_direction_the_vocabulary_allows() -> None:
    """A registry row whose direction is a typo would refuse every campaign naming it.

    The check that keeps the two halves speaking one language: `registered_direction` is compared
    for *equality* against `ObjectiveSpec.direction`, so a row spelling it `"max"` would make its
    objective permanently unstartable — and the refusal would blame the caller. Asserted over the
    registry rather than over a list written here, so a new objective is covered on the day it is
    added.
    """
    from chemclaw.science.bo.objectives import _REGISTRY, registered_direction

    assert _REGISTRY, "an empty registry would make this check vacuous"
    for name in _REGISTRY:
        assert registered_direction(name) in {"maximize", "minimize"}, (
            f"objective {name!r} declares direction {registered_direction(name)!r}, which no "
            "`ObjectiveSpec` can equal — every campaign naming it would be refused"
        )


def test_a_running_campaign_records_each_round_not_only_its_ending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything a running campaign has paid for must be recoverable before it ends.

    The write used to happen once, after the round loop. Until then every completed round lived
    only in Temporal's event history, so a campaign cancelled, terminated, or failed
    non-retryably mid-run answered `resume_campaign` with "no such campaign" about hours of real
    evaluation — the same gap the terminal write closed for a campaign that *finishes*, left open
    for every other ending.

    Driven through the real workflow rather than by calling the activity in a loop, because the
    property under test is the workflow's own sequencing: the record has to land *between* rounds,
    and an activity called directly cannot show that it does.

    Two rounds, so the assertion distinguishes per-round from once-at-the-end. Three suggestions
    result: one per round, plus the terminal recommendation. The round rows are keyed
    `"{workflow_id}:r{n}"`, because `record_suggestion` dedupes on `(campaign_id, job_id)` and a
    per-round write under the bare workflow id would collide with round 1 and drop the rest.
    """
    store = InMemoryCampaignStore()
    monkeypatch.setattr("chemclaw.science.bo.campaign_record.campaign_store", lambda: store)
    campaign_store.cache_clear()

    async def _drive() -> None:
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
                task_queue="test-bo-rounds",
                workflows=[BoCampaignWorkflow],
                activities=_BO_ACTIVITIES,
            ):
                await client.execute_workflow(
                    BoCampaignWorkflow.run,
                    spec.model_dump(mode="json"),
                    id="bo-campaign-rounds",
                    task_queue="test-bo-rounds",
                )

    asyncio.run(_drive())

    campaign_id = campaign_id_for(build_problem(load_dataset()))
    recorded = asyncio.run(store.suggestions_for(campaign_id, limit=10))
    job_ids = {suggestion.job_id for suggestion in recorded}
    assert job_ids == {
        "bo-campaign-rounds:r1",
        "bo-campaign-rounds:r2",
        "bo-campaign-rounds",
    }, "one row per completed round, plus the terminal recommendation"

    # Each round's row carries the history as it stood *then*, which is what makes a resume from a
    # killed campaign pick up where the evaluation actually got to rather than at the seed.
    by_job = {suggestion.job_id: suggestion for suggestion in recorded}
    assert len(by_job["bo-campaign-rounds:r1"].observations) == 5  # 4 seed + round 1
    assert len(by_job["bo-campaign-rounds:r2"].observations) == 6  # + round 2

    # The round rows are the only place a campaign's surrogate belief reaches the database: the
    # terminal row records the best *point*, which has no prediction attached to it.
    assert any(
        candidate.predicted_value is not None
        for row in ("bo-campaign-rounds:r1", "bo-campaign-rounds:r2")
        for candidate in by_job[row].candidates
    ), "a proposed candidate carries what the surrogate expected of it"


async def _wait_started(handle: Any, *, tries: int = 400) -> None:
    """Block until `handle` names a run the server has actually started.

    A child workflow is created by its parent's *next* workflow task, so a status read taken the
    instant the parent starts can land before the wait exists at all.
    """
    for _ in range(tries):
        try:
            await handle.describe()
            return
        except Exception:
            # The handle simply does not name a run yet — the parent has not scheduled the child.
            await asyncio.sleep(0.05)
    raise AssertionError(f"{handle.id} never opened, so there was no wait to measure")


async def _left_running(handle: Any, *, tries: int = 400) -> Any:
    """Block until `handle` reaches a terminal status, and return its description."""
    for _ in range(tries):
        described = await handle.describe()
        if described.status != WorkflowExecutionStatus.RUNNING:
            return described
        await asyncio.sleep(0.05)
    raise AssertionError(f"{handle.id} never left RUNNING")


def test_a_measured_campaign_outlives_the_ceiling_that_would_have_killed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measured campaign must survive its own wait, and must settle it when it does not.

    **The defect this pins.** `_measure` suspends on `AwaitAnswerWorkflow` for
    `bo_measurement_deadline_days` — fourteen days, a plate turnaround — inside a child that
    `ConnectorJobWorkflow` bounds at `connector_job_timeout_seconds`, five hours. The wait was 67x
    the ceiling above it, so the one campaign shape the durable wait was built for could not reach
    its own deadline: five hours in the child is `TIMED_OUT`, the wrapper reports
    `job_failed reason="Timed out"`, and every already-paid round is lost.

    **Two arms, one server, because the claim is a difference.** The first arm is the shipped
    arithmetic scaled 1:one — a real `BoCampaignWorkflow` on a real broker under the ceiling
    `child_execution_timeout` used to hand it — and it must die with the wait still open. The second
    is the same campaign under the bound the same function resolves now, and it must live long
    enough to be answered. Reading either alone proves nothing: the first is only a defect because
    the second is possible, and the second is only a fix because the first fails.

    **And the first arm measures the second half of the change.** A campaign killed by anything
    other than completing used to strand its wait: `execute_child_workflow` defaults to
    `ParentClosePolicy.TERMINATE`, a terminate never resumes workflow code, so `run`'s
    `except asyncio.CancelledError` — the whole reason `_settle` has a detached form — was
    unreachable from this call site and the `pending_requests` row stayed `waiting` in every
    entitled person's inbox for ever. `CANCELED` rather than `TERMINATED` is the deterministic
    half of that and the one that discriminates; whether the settle itself lands before the run
    closes is a race `tests/test_awaiting.py` declines to assert, for the reason stated there.

    Real-time rather than time-skipping: an idle time-skipping server fast-forwards to the wait's
    own deadline, which would expire both arms before either could be answered. Unsandboxed for
    the reason `tests/test_awaiting.py` is — the deadline and the queue are read off `settings`
    inside workflow code, and this drives them from the test.
    """
    ceiling = timedelta(seconds=4)
    queue = "test-bo-measured"
    monkeypatch.setattr(settings, "background_task_queue", queue)
    # Long enough that neither arm expires on its own inside the test, so the only thing that can
    # end arm one is the ceiling under test.
    monkeypatch.setattr(settings, "bo_measurement_deadline_days", 300 / 86_400)

    spec = CampaignSpec(
        problem=build_problem(load_dataset()),
        objective_name=MEASURED_OBJECTIVE,
        n_initial=4,
        n_rounds=0,
    )

    async def _run() -> None:
        answer = [
            Observation(params=candidate.params, value=float(i), provenance="measured")
            for i, candidate in enumerate(await propose_initial(spec.problem, 4, spec.seed))
        ]
        async with await start_local_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue=queue,
                workflows=[BoCampaignWorkflow, AwaitAnswerWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[*_BO_ACTIVITIES, *_projection_stubs()],
            ):
                arms = {
                    name: await client.start_workflow(
                        BoCampaignWorkflow.run,
                        spec.model_dump(mode="json"),
                        id=f"bo-measured-{name}",
                        task_queue=queue,
                        execution_timeout=bound,
                    )
                    for name, bound in (
                        ("under-the-old-ceiling", ceiling),
                        ("under-the-resolved-bound", child_execution_timeout(None, True)),
                    )
                }
                waits = {
                    name: client.get_workflow_handle(f"{handle.id}:await:seed")
                    for name, handle in arms.items()
                }
                for wait in waits.values():
                    await _wait_started(wait)

                killed = await _left_running(arms["under-the-old-ceiling"])
                assert killed.status == WorkflowExecutionStatus.TIMED_OUT, (
                    "the shipped ceiling did not kill the campaign, so this arm is not the defect "
                    "it claims to reproduce"
                )
                stranded = await _left_running(waits["under-the-old-ceiling"])
                assert stranded.status == WorkflowExecutionStatus.CANCELED, (
                    f"the killed campaign left its wait {stranded.status.name}; only a "
                    "cancellation reaches `AwaitAnswerWorkflow.run`'s own handler, which is what "
                    "stops the `pending_requests` row saying `waiting` for the rest of the deadline"
                )

                await waits["under-the-resolved-bound"].signal(
                    AwaitAnswerWorkflow.provide,
                    {"answered_by": "oid-bench", "payload": {"observations": answer}},
                )
                envelope = await arms["under-the-resolved-bound"].result()
                lived = await arms["under-the-resolved-bound"].describe()

        result = CampaignResult.model_validate(envelope.data)
        assert [o.value for o in result.history] == [0.0, 1.0, 2.0, 3.0], (
            "the campaign did not run on the answer it was given, so surviving the ceiling bought "
            "nothing"
        )
        assert lived.close_time is not None and lived.close_time - lived.start_time > ceiling, (
            "the surviving arm finished inside the old ceiling, so this test would pass with the "
            "ceiling restored and is evidence about nothing"
        )

    asyncio.run(_run())


def _projection_stubs() -> list[Any]:
    """The wait's four projection activities, recorded rather than written to Postgres.

    `pending_requests` has its own tests; what this file needs is a wait that opens, holds and
    settles, and a real table would make an offline run depend on a database for a property that is
    about workflow lifetime. The open stub still has to *answer*: the real activity owns the clamp
    against `awaiting_max_days` and returns the deadline the workflow schedules its timers against,
    so a stub returning nothing fails the workflow on the first line that reads it.
    """

    async def _open(payload: Any) -> str:
        request = payload["request"] if isinstance(payload, dict) else payload.request
        days = request["deadline_days"] if isinstance(request, dict) else request.deadline_days
        started = payload["started_at"] if isinstance(payload, dict) else payload.started_at
        return (datetime.fromisoformat(started) + timedelta(days=float(days))).isoformat()

    async def _settle(_payload: Any) -> bool:
        return True

    async def _remind(_request_id: str) -> None: ...

    async def _notify(_payload: Any) -> None: ...

    return [
        activity.defn(name="open_pending_request_activity")(_open),
        activity.defn(name="settle_pending_request_activity")(_settle),
        activity.defn(name="record_reminder_activity")(_remind),
        activity.defn(name="record_session_event_activity")(_notify),
    ]
