"""Tests for the BO recommendation → knowledge-graph bridge (plan step 1d.5)."""

import pathlib
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import chemclaw.durable.memory_jobs as memory_jobs
from chemclaw.connectors.bo import activities as _bo_activities  # noqa: F401 (registers them)
from chemclaw.connectors.bo.knowledge import note_from_campaign_result
from chemclaw.connectors.bo.workflows import BoCampaignWorkflow
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobWorkflow
from chemclaw.durable.job_record import record_job
from chemclaw.durable.memory_jobs import publish_memory_note_activity
from chemclaw.durable.registry import registered_activities
from chemclaw.science.bo.problem import (
    CampaignResult,
    CampaignSpec,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from tests.conftest import FakeWriter
from tests.siblings import connector_manifest_files
from tests.temporal_env import pydantic_client, start_env_or_skip

# Taken from the registry rather than written out, so a new activity cannot be missing from this
# worker and leave a workflow task redelivering forever.
_BO_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities(bundle_queue("bo"))

_PROBLEM = OptimizationProblem(
    parameters=[
        CategoricalParameter(name="catalyst", categories=["P1", "P2"]),
        ContinuousParameter(name="temperature", lower=30.0, upper=110.0),
    ],
    objectives=[Objective(name="yield", direction="maximize")],
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


# A campaign that recommends *molecules*, by both routes the spec offers: a categorical whose
# levels are SMILES (what `molecule_library_problem` builds) and a featurized categorical carrying
# a label → SMILES map. What the note does with these is what the hazard gate can see.
_MOLECULE_PROBLEM = OptimizationProblem(
    parameters=[
        CategoricalParameter(name="molecule", categories=["CCCN=[N+]=[N-]", "CCCCO"]),
        CategoricalParameter(
            name="ligand", categories=["L1", "L2"], structures={"L1": "CCO", "L2": "CCOCC"}
        ),
        ContinuousParameter(name="temperature", lower=20.0, upper=100.0),
    ],
    objectives=[Objective(name="yield", direction="maximize")],
)

_MOLECULE_BEST = Observation(
    params={"molecule": "CCCN=[N+]=[N-]", "ligand": "L1", "temperature": 80.0},
    value=61.0,
    provenance="predicted",
    surrogate_sd=3.0,
)

_MOLECULE_RESULT = CampaignResult(best=_MOLECULE_BEST, history=[_MOLECULE_BEST])


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

    The note's reader has no other copy of the spec; it lives in the job record and Temporal's
    history.
    """
    body = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).body
    # Categorical options in full, not counted: "one of 2 catalysts" would not tell a reviewer
    # whether the catalyst they would have tried was even on the list.
    assert "catalyst: one of P1, P2" in body
    assert "temperature: 30 to 110" in body
    # And which way "better" runs, which decides whether the best point is a max or a min.
    assert "maximize `yield`" in body


def test_the_note_carries_the_molecules_it_recommends() -> None:
    """A recommendation has to name its structures *as* structures, or nothing downstream sees them.

    A `bo-candidate` proposes an unrun experiment, so its molecules must be findable and pasteable
    into a screen. Both routes are covered: a library-style `molecule` categorical whose levels are
    SMILES, and a featurized `ligand` with a label → SMILES `structures` map (which the
    searched-space listing does not print). Asserted on the markdown, which reviewers and extractors
    read.
    """
    note = note_from_campaign_result("azide_yield", _MOLECULE_PROBLEM, _MOLECULE_RESULT)
    assert "- molecule: `CCCN=[N+]=[N-]`" in note.body  # the level is itself a SMILES
    assert "- ligand: L1 (`CCO`)" in note.body  # the label alone resolves to nothing


def test_a_label_that_names_no_molecule_is_left_as_prose() -> None:
    """The backticks mark structures; a bare catalyst label is not one and gains nothing from them.

    RDKit is the arbiter, so the writer needs no "is this a SMILES?" heuristic of its own — and a
    campaign over `P1`/`P2` reads exactly as it did before.
    """
    body = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).body
    assert "- catalyst: P1" in body and "`P1`" not in body


def test_compound_smiles_is_set_only_when_one_molecule_is_recommended() -> None:
    """`compound_smiles` is what a by-compound search returns, so a wrong one is worse than none.

    `kg.conflicts` and `find_notes` key on it. Set only when the recommendation names exactly one
    molecule, as in `ingest/eln/record.py::_principal_product`: a point naming a ligand and a
    substrate has no single subject.
    """
    from tests.bo_harness import molecule_library_problem

    library = molecule_library_problem(["CCCN=[N+]=[N-]", "CCCCO"])
    best = Observation(params={"molecule": "CCCN=[N+]=[N-]"}, value=-1.0, provenance="predicted")
    one = note_from_campaign_result(
        "solubility_max", library, CampaignResult(best=best, history=[best])
    )
    assert one.compound_smiles == "CCCN=[N+]=[N-]"
    # Two molecules recommended (a library level *and* a featurized ligand): no single subject.
    assert (
        note_from_campaign_result(
            "azide_yield", _MOLECULE_PROBLEM, _MOLECULE_RESULT
        ).compound_smiles
        is None
    )
    # No molecule at all: the campaign optimizes conditions, not a compound.
    assert note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).compound_smiles is None


def test_a_campaign_cannot_suppress_its_own_record() -> None:
    """A campaign cannot suppress its own record.

    Whether a campaign is remembered is the manifest's decision; no `CampaignSpec` field (which the
    model fills) may decide it.
    """
    assert "publish_to_graph" not in CampaignSpec.model_fields


def test_note_id_is_stable_for_the_same_recommendation() -> None:
    """The id is a hash of the recommended params, so re-proposing is idempotent."""
    assert (
        note_from_campaign_result("obj", _PROBLEM, _RESULT).id
        == note_from_campaign_result("obj", _PROBLEM, _RESULT).id
    )


async def test_campaign_publishes_recommendation_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished campaign writes a bo-candidate note (background queue).

    The core worker must register `ConnectorJobWorkflow` as well as the activities; without it the
    workflow task is never completed and `execute_workflow` waits forever without burning CPU.
    """
    fake = FakeWriter()
    # The gate is core's now, so the submitter is patched where core publishes from.
    monkeypatch.setattr(memory_jobs, "default_writer", lambda: fake)

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
    assert len(fake.writes) == 1  # the recommendation was proposed as a note
    assert fake.writes[0].files[0].path.startswith("knowledge/bo-candidate/bo-")


def test_a_library_campaigns_note_stays_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A screening library is one categorical with hundreds of levels — not a note line.

    Bounded by the shared note-excerpt budget, with the omitted count stated; the full space stays
    in the run record.
    """
    from tests.bo_harness import molecule_library_problem

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


def test_the_note_says_how_sure_the_surrogate_was_of_what_it_recommends() -> None:
    """The note says how sure the surrogate was of what it recommends.

    BoFire's posterior sd separates exploitation from extrapolation, the question a chemist asks
    before committing lab time.
    """
    proposed = _RESULT.model_copy(
        update={
            "best": _RESULT.best.model_copy(
                update={"provenance": "predicted", "surrogate_sd": 4.25}
            )
        }
    )
    body = note_from_campaign_result("reizman_suzuki", _PROBLEM, proposed).body
    line = next(ln for ln in body.splitlines() if ln.startswith("- objective value:"))
    assert "4.25" in line
    assert "surrogate posterior sd" in line


def test_a_seed_point_says_no_model_proposed_it_rather_than_staying_quiet() -> None:
    """Absence of a sd is a claim, not a gap: nothing had an opinion yet.

    A space-filling seed can win a campaign outright, and a note that simply omits the surrogate
    line there would read as an endorsement by the model of a point the model never saw.
    """
    body = note_from_campaign_result("reizman_suzuki", _PROBLEM, _RESULT).body
    line = next(ln for ln in body.splitlines() if ln.startswith("- objective value:"))
    assert _RESULT.best.surrogate_sd is None
    assert "space-filling seed" in line
    assert "surrogate posterior sd" not in line


def test_the_recommended_value_survives_the_excerpt_a_reader_actually_sees() -> None:
    """The recommended value survives the excerpt a reader actually sees.

    `_excerpt` is a blind prefix at `note_excerpt_chars`, so the value line sits above the
    conditions list.
    """
    from chemclaw.retrieval.retrievers import _excerpt

    wide = OptimizationProblem(
        parameters=[
            ContinuousParameter(name=f"reagent_equivalents_{i}", lower=0.5, upper=5.0)
            for i in range(8)
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    best = Observation(
        params={f"reagent_equivalents_{i}": 1.0 + i for i in range(8)},
        value=98.7,
        provenance="predicted",
        surrogate_sd=4.25,
    )
    body = note_from_campaign_result(
        "wide_screen", wide, CampaignResult(best=best, history=[best])
    ).body
    # The conditions block alone overruns the excerpt budget, which is the situation being fixed.
    assert len(body) > settings.note_excerpt_chars
    excerpt = _excerpt(body)
    assert "98.7" in excerpt, "the excerpt quotes conditions without the value they achieved"
    assert "surrogate posterior sd" in excerpt


#: What a model-facing description must never say about this bundle's note, one phrase per claim.
#:
#: Narrow on purpose: "these are proposals a human runs" is true. What is forbidden is the claim
#: that a reviewer stands between the note and the graph, because none does.
_REPO = pathlib.Path(__file__).resolve().parents[1]

_GATE_CLAIMS = ("pr-gated", "pr gate", "pull request", "human review", "before it enters the graph")

#: Lines in the corpus below that name the gate in order to say it is **gone**.
#:
#: Keyed by `path:line-text`, so a file cannot pick up a second, live claim under an exemption for a
#: historical one. The phrase is a fragment of the matching line, so a reflow does not break it.
_GATE_CLAIM_HISTORICAL = {
    "safety-screening/SKILL.md": "and the PR gate over agent-written knowledge",
}


def test_no_model_facing_bo_text_claims_a_recommendation_is_reviewed_before_it_lands() -> None:
    """No model-facing BO text claims a recommendation is reviewed before it lands.

    Notes are written straight into the graph with `created_by: agent`
    (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`), so a description promising human
    review overstates safety. Whoever re-adds the claim must add the review too.
    """
    corpus = _model_facing_text()
    assert len(corpus) > 1, (
        f"this scan found {len(corpus)} model-facing file(s); the globs below have stopped "
        "resolving, and an absence test over an empty corpus passes by saying nothing"
    )
    offenders = []
    for path in corpus:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not any(claim in line.lower() for claim in _GATE_CLAIMS):
                continue
            if _GATE_CLAIM_HISTORICAL.get(f"{path.parent.name}/{path.name}", "\0") in line:
                continue
            offenders.append(f"{path.relative_to(_REPO)}:{number}: {line.strip()}")
    assert not offenders, "model-facing text claims a review gate that no longer exists:\n" + (
        "\n".join(offenders)
    )


def test_no_historical_gate_exemption_is_unspent() -> None:
    """An exemption whose line has gone is a permission nobody spends — the register's other half.

    A row whose line was reworded or deleted would go on silencing whatever lands at that path next.
    """
    corpus = {f"{path.parent.name}/{path.name}": path.read_text() for path in _model_facing_text()}
    unspent = sorted(
        key for key, phrase in _GATE_CLAIM_HISTORICAL.items() if phrase not in corpus.get(key, "")
    )
    assert not unspent, (
        f"exemption(s) naming a line that is no longer there: {unspent}. Delete the row — the file "
        "either stopped mentioning the gate or now mentions it differently, and in the second case "
        "the new wording has to be read before it is exempted."
    )


def _model_facing_text() -> list[pathlib.Path]:
    """Every file whose words reach the model: a tool description, or an injected skill.

    The claim refused is not BO-specific, so the corpus is every `connector.yaml` description (the
    fleet's, from the installed package, and this tree's) and every bundled or root `SKILL.md`.
    """
    return [
        *connector_manifest_files(),
        *sorted(_REPO.glob("src/chemclaw/connectors/*/skills/**/SKILL.md")),
        *sorted(_REPO.glob("skills/**/SKILL.md")),
    ]
