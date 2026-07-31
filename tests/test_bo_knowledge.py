"""Tests for the BO recommendation → knowledge-graph bridge (plan step 1d.5)."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.memory_jobs as memory_jobs
from chemclaw.connectors.bo.activities import evaluate_candidates, propose_initial, propose_next
from chemclaw.connectors.bo.knowledge import note_from_campaign_result
from chemclaw.connectors.bo.workflows import BoCampaignWorkflow
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobWorkflow
from chemclaw.durable.job_record import record_job
from chemclaw.durable.memory_jobs import publish_memory_note_activity
from chemclaw.science.bo.problem import (
    CampaignResult,
    CampaignSpec,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip

_BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]

_PROBLEM = OptimizationProblem(
    parameters=[
        CategoricalParameter(name="catalyst", categories=["P1", "P2"]),
        ContinuousParameter(name="temperature", lower=30.0, upper=110.0),
    ],
    objective=Objective(name="yield", direction="maximize"),
)

_RESULT = CampaignResult(
    best=Observation(
        params={"catalyst": "P1", "temperature": 90.0}, value=98.7, provenance="measured"
    ),
    history=[
        Observation(params={"catalyst": "P2", "temperature": 30.0}, value=12.0),
        Observation(
            params={"catalyst": "P1", "temperature": 90.0}, value=98.7, provenance="measured"
        ),
    ],
)


def test_note_from_campaign_result_maps_fields() -> None:
    """The recommendation becomes an agent `bo-candidate` note with conditions + provenance."""
    note = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT)
    assert note.type == "bo-candidate"
    assert note.created_by == "agent"
    assert note.source == "bo:reizman_suzuki"
    assert note.id.startswith("bo-reizman_suzuki-")
    assert "catalyst: P1" in note.body and "temperature: 90" in note.body
    assert "98.7" in note.body and "measured" in note.body
    assert "2 evaluation" in note.body  # cites how many evaluations backed it
    # No dangling wikilink (would fail kg-validate on this PR).
    assert note.outgoing_links() == []


def test_the_note_says_what_space_was_searched() -> None:
    """A recommended value is uninterpretable without the range it was chosen from (D-157).

    "1.2 mol% Pd" means one thing when the campaign could have gone to 5 and another when 1.2 was
    the ceiling — and the person reading the merged markdown has no other copy of the spec: it
    lives in the job record and in Temporal's history, neither of which is in front of a reviewer.
    """
    body = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).body
    # Categorical options in full, not counted: "one of 2 catalysts" would not tell a reviewer
    # whether the catalyst they would have tried was even on the list.
    assert "catalyst: one of P1, P2" in body
    assert "temperature: 30 to 110" in body
    # And which way "better" runs, which decides whether the best point is a max or a min.
    assert "maximize `yield`" in body


def test_a_campaign_cannot_suppress_its_own_record() -> None:
    """`publish_to_graph` on the spec was a model-authored switch over a deployment's decision.

    Default `False` and filled in by the LLM, it silently suppressed the only permanent artifact a
    campaign produced — after which the result expired with Temporal's history and the run left no
    trace at all. The decision is the manifest's alone now, and this pins that the field does not
    come back: nothing in `CampaignSpec` may decide whether the campaign is remembered.
    """
    assert "publish_to_graph" not in CampaignSpec.model_fields


def test_note_id_is_stable_for_the_same_recommendation() -> None:
    """The id is a hash of the recommended params, so re-proposing is idempotent."""
    assert (
        note_from_campaign_result("obj", _PROBLEM, _RESULT).id
        == note_from_campaign_result("obj", _PROBLEM, _RESULT).id
    )


def test_campaign_publishes_recommendation_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """With publish_to_graph, a finished campaign proposes a bo-candidate note (bg queue).

    This test carried `@pytest.mark.timeout(600)` on the reasoning that it is "slow, not hung"
    because it fits a real BoTorch GP inside a Temporal worker. That reasoning was wrong, and it
    kept `main` red: the core worker below registered activities only, so the
    `ConnectorJobWorkflow` submitted to its queue had no registered handler and its workflow task
    was never completed. `execute_workflow` then waits forever — and it is a *cheap* forever, with
    no CPU burnt, which is why "slow" looked plausible.

    Measured against the live Temporal dev server, with `background_task_queue` pointed at a private
    name so no other worker could serve it: without `workflows=[ConnectorJobWorkflow]` the call
    hung past 100 s; with it, the campaign completed in well under the 180 s global cap. So the
    override is gone too — this test does not need one.
    """
    fake = FakeSubmitter()
    # The gate is core's now, so the submitter is patched where core publishes from.
    monkeypatch.setattr(memory_jobs, "default_submitter", lambda: fake)

    async def _run() -> None:
        from chemclaw.science.bo.benchmarks.reizman_suzuki import build_problem, load_dataset

        spec = CampaignSpec(
            problem=build_problem(load_dataset()),
            objective_name="reizman_suzuki",
            n_initial=3,
            n_rounds=1,
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-bo-pub",
                    workflows=[BoCampaignWorkflow],
                    activities=_BO_ACTIVITIES,
                ),
                # Core's wrapper runs HERE, so this worker must register it. Registering only
                # the activity is what hung: Temporal keeps redelivering a workflow task whose
                # type no worker knows, and the caller waits on a result that can never arrive.
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    workflows=[ConnectorJobWorkflow],
                    activities=[publish_memory_note_activity, record_job],
                ),
            ):
                # The campaign now *builds* the note and core *publishes* it, so this drives the
                # whole path: the connector's workflow as a child of core's wrapper, which PR-gates
                # whatever note the envelope carries (D-093).
                await client.execute_workflow(
                    ConnectorJobWorkflow.run,
                    ConnectorJobInput(
                        connector="bo",
                        job="start_optimization_campaign",
                        workflow="BoCampaignWorkflow",
                        task_queue="test-bo-pub",
                        payload=spec.model_dump(mode="json"),
                        requested_by="tester",
                        rationale="find a higher-yielding condition set for the teaching example",
                        publish_to_graph=True,
                    ),
                    id="bo-publish-test",
                    task_queue=settings.background_task_queue,
                )
        assert len(fake.submissions) == 1  # the recommendation was proposed as a note
        assert fake.submissions[0].files[0].path.startswith("knowledge/bo-candidate/bo-")

    asyncio.run(_run())


def test_a_library_campaigns_note_stays_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A screening library is one categorical with hundreds of levels — not a note line.

    `molecule_library_problem` makes every SMILES in the library a level, so listing them all put
    a single multi-kilobyte line into the note a chemist reads to approve *one* experiment
    (review of D-157). Bounded by the shared note-excerpt budget, with the omitted count stated
    and the complete space still in the run record.
    """
    from chemclaw.science.bo.objectives import molecule_library_problem

    library = [
        f"{'C' * (1 + index // 6)}c1ccc({'O' * (index % 6) or 'N'}C)cc1" for index in range(60)
    ]
    problem = molecule_library_problem(library)
    # Asserted, not cast: the whole point of this case is that the library *is* one categorical
    # with a level per molecule, so a change that made it anything else should fail here loudly.
    parameter = problem.parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    levels = parameter.categories
    result = CampaignResult(
        best=Observation(params={"molecule": levels[0]}, value=-1.2, provenance="predicted"),
        history=[
            Observation(params={"molecule": s}, value=-2.0, provenance="predicted")
            for s in levels[:9]
        ],
    )

    body = note_from_campaign_result("solubility_max", problem, result).body
    (line,) = [ln for ln in body.splitlines() if ln.startswith("- molecule: one of")]

    assert len(line) <= settings.note_excerpt_chars + 120  # the listing, plus the "+N more" tail
    assert "more; the full set is in the run record" in line
    # Bounded, not emptied: the first levels are still named, which is what makes the line useful.
    assert levels[0] in line
    # A small space is still listed in full, with no truncation tail invented for it.
    small = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).body
    assert "catalyst: one of P1, P2" in small and "more" not in small
