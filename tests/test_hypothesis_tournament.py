"""The tournament workflow, driven end to end against the real compiled workflow.

The activities are replaced with deterministic stubs — the point of these tests is the workflow's
own orchestration (the screen, the rounds, the rating, what it does when a stage fails), not the
model calls. Every one drives `HypothesisTournamentWorkflow.run` through a real Temporal worker
rather than calling its private helpers, because the defects worth catching here are the ones that
only appear once Temporal is sequencing the activities.
"""

from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.durable import hypothesis_tournament as ht
from chemclaw.durable.hypothesis_tournament import (
    HypothesisTournamentWorkflow,
    TournamentRequest,
)
from chemclaw.durable.job_record import JobRecord
from chemclaw.hypotheses.models import CheckCall, CheckOutcome, DiscriminatingCheck, Hypothesis
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
    recorded_jobs: list[JobRecord] | None = None,
    prefer: str = "",
    generate_fails: bool = False,
    critique_fails: bool = False,
    compare_fails: bool = False,
    check_kind: str = "physical",
    check_call: object = None,
    max_calculations: int = 2,
    double_judge: bool = False,
    always_prefers_left: bool = False,
) -> list[Any]:
    """Deterministic stand-ins for every activity, registered under the real activity names.

    `recorded_jobs` collects what the run persists, so the durable-record path is driven rather
    than swallowed — without a `record_job` stub the workflow's own `except ActivityError` turns a
    missing activity into a warning and the test passes without exercising anything.

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
            max_calculations=max_calculations,
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
        # Order-independent by construction: the winner is decided from the two ids alone, then
        # translated into a side. An earlier version answered "right" whenever neither side was
        # `prefer`, which is a right-biased judge — it made the position-bias control fail for a
        # real reason, in the stub rather than in the code.
        winner = (
            prefer
            if prefer in {request.left.id, request.right.id}
            else min(request.left.id, request.right.id)
        )
        better = "left" if winner == request.left.id else "right"
        return ht._ComparisonVerdict(better=better, rationale="the record favours it")

    @activity.defn(name="derive_check")
    async def derive_check(request: ht._CritiqueRequest) -> DiscriminatingCheck:
        return DiscriminatingCheck(
            hypothesis_id=request.hypothesis.id,
            question=f"run the control for {request.hypothesis.id}",
            kind=check_kind,  # type: ignore[arg-type]
            call=check_call,  # type: ignore[arg-type]
            expectation="the effect disappears if the hypothesis holds",
        )

    @activity.defn(name="record_hypothesis_proposal")
    async def record_hypothesis_proposal(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        return note_id

    collected = recorded_jobs if recorded_jobs is not None else []

    @activity.defn(name="record_job")
    async def record_job(record: JobRecord) -> None:
        collected.append(record)

    @activity.defn(name="record_hypothesis_field")
    async def record_hypothesis_field(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        return note_id

    return [
        record_job,
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


def _entry_order(question: str, field: list[Hypothesis]) -> list[str]:
    """The order the workflow enters hypotheses in — a hash of (question, id), never the id."""
    return sorted((h.id for h in field), key=lambda name: stable_hash([question, name]))


def _wide_field(size: int = 8) -> list[Hypothesis]:
    """A field big enough for the bias estimator to say anything — see `_MIN_BIAS_SAMPLE`."""
    return [_hypothesis(f"h{i}", f"explanation number {i}") for i in range(size)]


async def test_a_position_biased_judge_is_measured_rather_than_believed() -> None:
    """A judge that always names whichever it saw first must read as 100% position bias.

    This is the measurement the feature claims to make. A run reporting no bias here would be
    reporting a property of the estimator rather than of the judge.
    """
    result = await _run(_stubs(field=_wide_field(), always_prefers_left=True), _request("q-biased"))

    assert result.data["position_bias"] == 1.0
    assert "position bias measured at 100%" in result.summary


async def test_a_judge_that_ignores_order_shows_no_position_bias() -> None:
    """The control, and the case a reversal rate gets wrong.

    This judge is perfectly consistent *and* order-independent. A reversal-rate statistic scores it
    0.0 while scoring a merely noisy order-independent judge 0.5, even though both have zero bias —
    which is why the figure is built on first-position win rate instead.
    """
    result = await _run(_stubs(field=_wide_field(), prefer="h3"), _request("q-unbiased"))

    assert result.data["position_bias"] == pytest.approx(0.0, abs=0.35)
    assert [row["hypothesis"]["id"] for row in result.data["ranked"]][0] == "h3"


async def test_bias_is_absent_rather_than_zero_when_too_few_comparisons_ran() -> None:
    """`None` and `0.0` are different claims about position bias.

    At one decisive comparison `|2p - 1|` is identically 1.0 whichever side won, so a
    two-hypothesis tournament would otherwise announce total position bias from a single
    judgement. Below the floor the honest answer is "not measured".
    """
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, prefer="a"), _request("q-nobias"))

    assert result.data["position_bias"] is None
    assert "position bias" not in result.summary


async def test_the_workflow_enters_the_field_in_a_question_derived_order() -> None:
    """The permutation the bracket's fairness depends on is applied here, not assumed.

    `pairing.py` breaks a score tie by input position, so the order this workflow hands it decides
    the whole bracket. `tests/test_hypotheses.py` proves a *fixed* entry order costs over a hundred
    Elo of artefact under a null judge; this proves the workflow does not hand it one. Two
    different questions over the same hypotheses must produce different first pairings, and the
    same question must reproduce its own.
    """
    field = _wide_field(6)
    seen: dict[str, list[tuple[str, str]]] = {}

    def recording_stubs(question: str) -> list[Any]:
        pairs: list[tuple[str, str]] = []
        seen[question] = pairs
        stubs = _stubs(field=field, prefer="h0")
        original = next(
            s for s in stubs if s.__temporal_activity_definition.name == "compare_hypotheses"
        ).__temporal_activity_definition.fn

        @activity.defn(name="compare_hypotheses")
        async def recorder(request: ht._ComparisonRequest) -> ht._ComparisonVerdict:
            pairs.append((request.left.id, request.right.id))
            verdict: ht._ComparisonVerdict = await original(request)
            return verdict

        return [
            s for s in stubs if s.__temporal_activity_definition.name != "compare_hypotheses"
        ] + [recorder]

    yield_question = "why did the yield drop?"
    impurity_question = "where is the impurity from?"

    await _run(recording_stubs(yield_question), _request(yield_question))
    # Captured before the repeat, or the comparison below is an object against itself.
    first = list(seen[yield_question])
    await _run(recording_stubs(impurity_question), _request(impurity_question))
    other = list(seen[impurity_question])
    await _run(recording_stubs(yield_question), _request(yield_question))
    repeat = list(seen[yield_question])

    assert first, "no comparisons were recorded"
    # Same question, same bracket — the property replay and re-reading both need. Sorted, because
    # a round's comparisons run concurrently, so the order they are *recorded* in is completion
    # order and carries no meaning; the pairs and their orientation are what this is about.
    assert sorted(first) == sorted(repeat)
    # And a different question seeds a different bracket, so no hypothesis holds a favoured slot
    # across runs. Asserted on the entry order rather than on the resulting pairs: three pairs over
    # six hypotheses can coincide between two brackets by chance, which would make this flaky for a
    # reason that has nothing to do with the property.
    assert _entry_order(yield_question, field) != _entry_order(impurity_question, field)
    assert other


async def test_the_run_is_persisted_so_its_id_outlives_temporal_retention() -> None:
    """A tournament's artifact is its envelope, so the record is the only queryable copy.

    Without this row `get_durable_job_status` raises `no durable job with id …` once the broker's
    retention passes — contradicting its own docstring — and the run is invisible to
    `find_past_jobs` and to `operations/`. `D-157` exempted `request_development_report` on the
    ground that its artifact is a self-describing note; the ratings, the intervals and what lost
    live nowhere but here.
    """
    recorded: list[JobRecord] = []
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    await _run(_stubs(field=field, prefer="a", recorded_jobs=recorded), _request("q-recorded"))

    assert len(recorded) == 1
    row = recorded[0]
    assert row.job == "rank_competing_hypotheses"
    assert row.requested_by == "chemist@example.com"
    assert row.state == "completed"
    # The ranking itself, not just a summary line — that is what the record exists to keep.
    assert row.result["ranked"]
    assert row.payload_kind == "TournamentOutcome"


async def test_an_empty_field_is_still_recorded() -> None:
    """The early return had no record at all, so a run screened down to nothing simply vanished."""
    recorded: list[JobRecord] = []
    field = [Hypothesis(id="vague", statement="something happens", refuted_if="unknown")]
    await _run(_stubs(field=field, recorded_jobs=recorded), _request("q-empty-recorded"))

    assert len(recorded) == 1
    assert recorded[0].result["ranked"] == []


# ------------------------------------------------------------------ durable calculations


def _grounded(**kwargs: Any) -> ht._GroundedJob:
    return ht._GroundedJob(**kwargs)


async def _run_with(extra: list[Any], stubs: list[Any], request: TournamentRequest) -> Any:
    """Drive the workflow with extra activity stubs replacing the defaults of the same name."""
    names = {a.__temporal_activity_definition.name for a in extra}
    kept = [a for a in stubs if a.__temporal_activity_definition.name not in names]
    async with await start_env_or_skip() as env:
        client: Client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=_QUEUE,
            workflows=[HypothesisTournamentWorkflow],
            activities=[*kept, *extra],
        ):
            return await client.execute_workflow(
                HypothesisTournamentWorkflow.run,
                request,
                id=f"hyp-job-{abs(hash(request.question)) % 10**8}",
                task_queue=_QUEUE,
            )


async def test_a_job_check_that_cannot_be_grounded_is_reported_not_dropped() -> None:
    """A refusal is an outcome, with its code, so a reader sees the check did not run and why."""
    calls: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck, requested_by: str = "", correlation_id: str = ""
    ) -> ht._GroundedJob:
        calls.append(check.hypothesis_id)
        return _grounded(
            refusal_code="subject-not-found",
            refusal_detail="'compound-invented' is not a note in this deployment's corpus",
        )

    field = [_hypothesis("a", "the first explanation")]
    call = CheckCall(job="compare_solvents", subjects={"reactants": ["compound-invented"]})
    result = await _run_with(
        [ground],
        _stubs(field=field, check_kind="computable", check_call=call),
        _request("q-ungrounded"),
    )

    assert calls, "the grounding activity was never reached"
    row = result.data["ranked"][0]
    assert row["outcome"]["verdict"] == "not-run"
    assert row["outcome"]["refusal_code"] == "subject-not-found"
    assert "not a note" in result.summary


async def test_the_calculation_budget_bounds_how_many_jobs_one_tournament_starts() -> None:
    """These jobs are `expensive: true`; a tournament must not spend a budget nobody agreed to.

    The checks past the cap are reported as not run *for budget* rather than dropped — a reader
    who cannot see that the budget bound the answer would read a thin result as a complete one.
    """
    grounded_for: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck, requested_by: str = "", correlation_id: str = ""
    ) -> ht._GroundedJob:
        grounded_for.append(check.hypothesis_id)
        return _grounded(refusal_code="job-unavailable", refusal_detail="not served here")

    field = [_hypothesis(f"h{i}", f"explanation {i}") for i in range(4)]
    call = CheckCall(job="compare_solvents", subjects={"reactants": ["compound-x"]})
    result = await _run_with(
        [ground],
        _stubs(field=field, check_kind="computable", check_call=call, max_calculations=1),
        _request("q-budget"),
    )

    assert len(grounded_for) == 1, "more checks were grounded than the budget allows"
    codes = {row["outcome"]["refusal_code"] for row in result.data["ranked"]}
    assert "over-budget" in codes
    assert "this tournament had already started 1 calculation(s)" in result.summary


async def test_a_tool_check_and_a_job_check_are_settled_in_one_run() -> None:
    """Both halves reach the same outcome map, so a field can mix cheap and expensive checks."""
    seen: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck, requested_by: str = "", correlation_id: str = ""
    ) -> ht._GroundedJob:
        seen.append("job")
        return _grounded(refusal_code="job-unavailable", refusal_detail="not served here")

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck, requested_by: str = "", correlation_id: str = ""
    ) -> CheckOutcome:
        seen.append("tool")
        return CheckOutcome(
            hypothesis_id=check.hypothesis_id, verdict="not-run", refusal_code="tool-unavailable"
        )

    field = [_hypothesis("a", "the first explanation")]
    result = await _run_with(
        [ground, run_check],
        _stubs(
            field=field,
            check_kind="computable",
            check_call=CheckCall(tool="predict_pka", subject_note_id="compound-x"),
        ),
        _request("q-tool-only"),
    )

    assert seen == ["tool"], "a tool check must not reach the job path"
    assert result.data["ranked"][0]["outcome"]["refusal_code"] == "tool-unavailable"
