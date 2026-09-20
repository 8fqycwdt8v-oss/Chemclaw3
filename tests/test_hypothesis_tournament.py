"""The tournament workflow, driven end to end against the real compiled workflow.

The activities are replaced with deterministic stubs — the point of these tests is the workflow's
own orchestration (the screen, the rounds, the rating, what it does when a stage fails), not the
model calls. Every one drives `HypothesisTournamentWorkflow.run` through a real Temporal worker
rather than calling its private helpers, because the defects worth catching here are the ones that
only appear once Temporal is sequencing the activities.
"""

from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.durable import hypothesis_tournament as ht
from chemclaw.durable.hypothesis_tournament import (
    HypothesisTournamentWorkflow,
    TournamentRequest,
)
from chemclaw.hypotheses.models import DiscriminatingCheck, Hypothesis
from tests.temporal_env import pydantic_client, start_env_or_skip

# The real background queue, not a test-local one. `publish_note_best_effort` pins its activity
# to `settings.background_task_queue` by design, so a worker on any other queue leaves the note
# write scheduled and unpolled and the workflow waits out its schedule-to-start timeout — measured,
# 13 activities scheduled and 12 started. Every other Temporal test in this suite does the same.
_QUEUE = settings.background_task_queue


def _hypothesis(name: str, statement: str, refuted: str = "") -> Hypothesis:
    return Hypothesis(
        id=name,
        statement=statement,
        refuted_if=refuted or f"no change is seen in {statement} after the control run",
    )


def _stubs(
    *,
    field: list[Hypothesis],
    prefer: str = "",
    generate_fails: bool = False,
    critique_fails: bool = False,
    compare_fails: bool = False,
    check_kind: str = "physical",
    double_judge: bool = False,
    always_prefers_left: bool = False,
) -> list[Any]:
    """Deterministic stand-ins for every activity, registered under the real activity names.

    `prefer` names the hypothesis the judge always picks, which is what makes an assertion about
    the resulting order meaningful rather than incidental.
    """

    @activity.defn(name="resolve_field_limits")
    async def resolve_field_limits() -> ht._FieldLimits:
        return ht._FieldLimits(
            angles=1,
            per_angle=len(field),
            max_hypotheses=10,
            double_judge_first_round=double_judge,
        )

    @activity.defn(name="draft_angles")
    async def draft_angles(request: TournamentRequest, wanted: int = 4) -> ht._AngleSet:
        return ht._AngleSet(angles=["one angle"])

    @activity.defn(name="gather_hypothesis_evidence")
    async def gather_hypothesis_evidence(
        query: str, hypothesis_id: str = "", requested_by: str = "", correlation_id: str = ""
    ) -> ht._EvidencePack:
        return ht._EvidencePack(hypothesis_id=hypothesis_id, framed=[], note_ids=[])

    @activity.defn(name="generate_hypotheses")
    async def generate_hypotheses(request: ht._GenerateRequest) -> ht._HypothesisBatch:
        if generate_fails:
            raise ValueError("generator unavailable")
        return ht._HypothesisBatch(hypotheses=field)

    @activity.defn(name="critique_hypothesis")
    async def critique_hypothesis(request: ht._CritiqueRequest) -> ht._ObjectionBatch:
        if critique_fails:
            raise ValueError("critic unavailable")
        return ht._ObjectionBatch(
            objections=[
                {
                    "hypothesis_id": request.hypothesis.id,
                    "concern": "untested at scale",
                    "rationale": "every run on file was at one gram",
                }  # type: ignore[list-item]
            ]
        )

    @activity.defn(name="compare_hypotheses")
    async def compare_hypotheses(request: ht._ComparisonRequest) -> ht._ComparisonVerdict:
        if compare_fails:
            raise ValueError("judge unavailable")
        if always_prefers_left:
            # A maximally position-biased judge: it names whichever hypothesis it saw first,
            # whatever the hypotheses are. Judged in both orders, every pair must reverse.
            return ht._ComparisonVerdict(better="left", rationale="it came first")
        if not prefer:
            return ht._ComparisonVerdict(better="tie", rationale="indistinguishable")
        better = "left" if request.left.id == prefer else "right"
        return ht._ComparisonVerdict(better=better, rationale="the record favours it")

    @activity.defn(name="derive_check")
    async def derive_check(request: ht._CritiqueRequest) -> DiscriminatingCheck:
        return DiscriminatingCheck(
            hypothesis_id=request.hypothesis.id,
            question=f"run the control for {request.hypothesis.id}",
            kind=check_kind,  # type: ignore[arg-type]
            expectation="the effect disappears if the hypothesis holds",
        )

    @activity.defn(name="record_hypothesis_proposal")
    async def record_hypothesis_proposal(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        return note_id

    @activity.defn(name="record_hypothesis_field")
    async def record_hypothesis_field(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        return note_id

    return [
        # The real fit, deliberately not stubbed: the ordering these tests assert is exactly what
        # it computes, and only the model calls are worth replacing.
        ht.fit_ratings,
        resolve_field_limits,
        draft_angles,
        gather_hypothesis_evidence,
        generate_hypotheses,
        critique_hypothesis,
        compare_hypotheses,
        derive_check,
        record_hypothesis_proposal,
        record_hypothesis_field,
    ]


async def _run(stubs: list[Any], request: TournamentRequest) -> Any:
    async with await start_env_or_skip() as env:
        client: Client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=_QUEUE,
            workflows=[HypothesisTournamentWorkflow],
            activities=stubs,
        ):
            return await client.execute_workflow(
                HypothesisTournamentWorkflow.run,
                request,
                id=f"hypotheses-test-{abs(hash(request.question)) % 10**8}",
                task_queue=_QUEUE,
            )


def _request(question: str = "why did the impurity appear?") -> TournamentRequest:
    return TournamentRequest(question=question, requested_by="chemist@example.com")


async def test_a_tournament_ranks_the_field_and_the_preferred_hypothesis_wins() -> None:
    """The whole path: generate, screen, critique, compare, rate, report."""
    field = [
        _hypothesis("thermal", "the impurity is thermal in origin"),
        _hypothesis("wet", "the solvent was wet"),
        _hypothesis("base", "the base decomposed"),
    ]
    result = await _run(_stubs(field=field, prefer="wet"), _request())

    ranked = result.data["ranked"]
    assert [row["hypothesis"]["id"] for row in ranked][0] == "wet"
    assert result.data["comparisons_run"] > 0
    assert all(row["comparisons"] >= 1 for row in ranked)
    assert result.payload_kind == "TournamentOutcome"


async def test_an_unfalsifiable_hypothesis_never_reaches_the_tournament() -> None:
    """The screen is the only stage that removes, and it reports what it removed."""
    field = [
        _hypothesis("good", "the impurity is thermal in origin"),
        Hypothesis(id="vague", statement="something else is happening", refuted_if="unknown"),
    ]
    result = await _run(_stubs(field=field), _request("q-screen"))

    assert [row["hypothesis"]["id"] for row in result.data["ranked"]] == ["good"]
    assert [r["hypothesis_id"] for r in result.data["rejected"]] == ["vague"]


async def test_a_field_the_judge_cannot_separate_is_reported_as_undecided() -> None:
    """A tie-ing judge must not produce a confident ordering."""
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, prefer=""), _request("q-tied"))

    assert result.data["leader_is_decisive"] is False
    assert "does not separate" in result.summary


async def test_a_physical_check_becomes_a_proposal_note_and_a_computable_one_does_not() -> None:
    """`kind` is the whole switch: a lab check is filed for a human, a tool check is not."""
    field = [_hypothesis("a", "the first explanation")]
    physical = await _run(_stubs(field=field, check_kind="physical"), _request("q-physical"))
    computable = await _run(_stubs(field=field, check_kind="computable"), _request("q-computable"))

    assert physical.data["proposal_note_ids"]
    assert not computable.data["proposal_note_ids"]


async def test_a_failing_critic_costs_objections_and_not_the_run() -> None:
    """The critique is advisory, so losing it must degrade the answer rather than the job."""
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(
        _stubs(field=field, prefer="a", critique_fails=True), _request("q-nocritic")
    )

    assert [row["hypothesis"]["id"] for row in result.data["ranked"]][0] == "a"
    assert all(not row["objections"] for row in result.data["ranked"])


async def test_a_failing_judge_leaves_every_hypothesis_unrated_rather_than_ordered() -> None:
    """With no comparison surviving, the prior is the whole answer and the table must say so.

    The dangerous failure is the opposite: an arbitrary order rendered as though it were judged.
    """
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, compare_fails=True), _request("q-nojudge"))

    assert result.data["comparisons_run"] == 0
    assert all(row["comparisons"] == 0 for row in result.data["ranked"])
    assert "unrated (never compared)" in result.summary


async def test_a_generator_that_produces_nothing_ends_cleanly() -> None:
    """An empty field is an answer, not a crash — and it must not claim to have ranked anything."""
    result = await _run(_stubs(field=[], generate_fails=True), _request("q-empty"))

    assert result.data["ranked"] == []
    assert "No hypotheses were generated" in result.summary


async def test_a_position_biased_judge_is_measured_rather_than_believed() -> None:
    """A judge that always names whichever it saw first must show up as 100% position bias.

    This is the whole reason the first round is judged in both orders. A run that reported no bias
    here would be reporting a property of the estimator rather than of the judge.
    """
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(
        _stubs(field=field, double_judge=True, always_prefers_left=True), _request("q-biased")
    )

    assert result.data["position_bias"] == 1.0
    assert "position bias measured at 100%" in result.summary


async def test_a_consistent_judge_shows_no_position_bias_and_still_ranks() -> None:
    """The control for the test above: same double-judging, a judge that ignores order."""
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, double_judge=True, prefer="a"), _request("q-unbiased"))

    assert result.data["position_bias"] == 0.0
    assert [row["hypothesis"]["id"] for row in result.data["ranked"]][0] == "a"


async def test_bias_is_absent_rather_than_zero_when_nothing_was_double_judged() -> None:
    """`None` and `0.0` are different claims about position bias.

    Absent means nothing was double-judged; zero means it was measured and found absent. The
    summary must not print the second when it only knows the first.
    """
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, prefer="a"), _request("q-nobias"))

    assert result.data["position_bias"] is None
    assert "position bias" not in result.summary
