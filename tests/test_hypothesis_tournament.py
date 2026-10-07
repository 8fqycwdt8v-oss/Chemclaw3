"""The tournament workflow, driven end to end against the real compiled workflow.

Activities are deterministic stubs; what is tested is the workflow's orchestration (screen,
rounds, rating, stage failures), through a real Temporal worker, since those defects appear only
once Temporal sequences the activities.
"""

from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
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
from chemclaw.durable.template_job import TemplateRunInput, TemplateRunResult
from chemclaw.hypotheses.models import CheckCall, CheckOutcome, DiscriminatingCheck, Hypothesis
from chemclaw.templates.registry import discovered
from tests.temporal_env import pydantic_client, start_env_or_skip

# The real background queue: `publish_note_best_effort` pins its activity there, so a worker on any
# other queue leaves the note write unpolled.
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

    `recorded_jobs` collects what the run persists; without a `record_job` stub the workflow would
    turn the missing activity into a warning. `prefer` names the hypothesis the judge always picks,
    so order assertions are meaningful.
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
        # Order-independent: the winner is decided from the two ids alone, then translated into a
        # side, so the stub is not position-biased.
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
    """A judge that ignores order shows no position bias.

    A reversal rate would score a noisy order-independent judge 0.5; the figure uses first-position
    win rate instead.
    """
    result = await _run(_stubs(field=_wide_field(), prefer="h3"), _request("q-unbiased"))

    assert result.data["position_bias"] == pytest.approx(0.0, abs=0.35)
    assert [row["hypothesis"]["id"] for row in result.data["ranked"]][0] == "h3"


async def test_bias_is_absent_rather_than_zero_when_too_few_comparisons_ran() -> None:
    """Position bias is absent rather than zero when too few comparisons ran.

    At one decisive comparison `|2p - 1|` is always 1.0; below the floor the answer is "not
    measured".
    """
    field = [
        _hypothesis("a", "the first explanation"),
        _hypothesis("b", "the second explanation"),
    ]
    result = await _run(_stubs(field=field, prefer="a"), _request("q-nobias"))

    assert result.data["position_bias"] is None
    assert "position bias" not in result.summary


async def test_the_workflow_enters_the_field_in_a_question_derived_order() -> None:
    """The workflow enters the field in a question-derived order.

    `pairing.py` breaks ties by input position, so the entry order decides the bracket. Two
    questions over the same hypotheses produce different orders, and one question reproduces its
    own.
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
    # Asserted on the entry order rather than the pairs, which can coincide by chance.
    assert _entry_order(yield_question, field) != _entry_order(impurity_question, field)
    assert other


async def test_the_run_is_persisted_so_its_id_outlives_temporal_retention() -> None:
    """The run is persisted, so its id outlives Temporal retention.

    The ratings, intervals and losers live only in the envelope; without the record row
    `get_durable_job_status` fails after retention and `find_past_jobs` cannot see the run.
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


async def _run_with(
    extra: list[Any],
    stubs: list[Any],
    request: TournamentRequest,
    children: list[Any] | None = None,
) -> Any:
    """Drive the workflow with extra activity stubs replacing the defaults of the same name.

    `children` registers stand-in child workflows on the same queue; a child nothing registered
    never starts.
    """
    names = {a.__temporal_activity_definition.name for a in extra}
    kept = [a for a in stubs if a.__temporal_activity_definition.name not in names]
    async with await start_env_or_skip() as env:
        client: Client = pydantic_client(env)
        async with Worker(
            client,
            task_queue=_QUEUE,
            workflows=[HypothesisTournamentWorkflow, *(children or [])],
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
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
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
    """The calculation budget bounds how many jobs one tournament starts.

    These jobs are `expensive: true`. Checks past the cap are reported as not run for budget rather
    than dropped, and the refusal claims nothing about whether the allowed checks actually started.
    """
    grounded_for: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
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
    assert "this tournament runs at most 1 calculation(s)" in result.summary


async def test_a_tool_check_and_a_job_check_are_settled_in_one_run() -> None:
    """Both halves reach the same outcome map, so a field can mix cheap and expensive checks."""
    seen: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> ht._GroundedJob:
        seen.append("job")
        return _grounded(refusal_code="job-unavailable", refusal_detail="not served here")

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
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


async def test_the_budget_is_spent_on_the_best_placed_checks_not_the_first_generated() -> None:
    """The budget is spent on the best-placed checks, not the first generated.

    Spending in generation order could refuse the leader's check while running the last-placed one.
    The judge always prefers `"wet"`, which is generated second.
    """
    grounded_for: list[str] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> ht._GroundedJob:
        grounded_for.append(check.hypothesis_id)
        return _grounded(refusal_code="job-unavailable", refusal_detail="not served here")

    field = [
        _hypothesis("thermal", "the impurity is thermal in origin"),
        _hypothesis("wet", "the solvent was wet"),
        _hypothesis("base", "the base decomposed"),
    ]
    call = CheckCall(job="compare_solvents", subjects={"reactants": ["compound-x"]})
    await _run_with(
        [ground],
        _stubs(
            field=field,
            prefer="wet",
            check_kind="computable",
            check_call=call,
            max_calculations=1,
        ),
        _request("q-order"),
    )

    assert grounded_for == ["wet"], (
        "the one calculation this tournament could afford went to a hypothesis the fit placed "
        "below the leader"
    )


async def test_the_budget_covers_tool_checks_too() -> None:
    """The budget covers tool checks too.

    A tool check is a real calculation on a cache miss and opens every connector session.
    """
    ran_for: list[str] = []

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> CheckOutcome:
        ran_for.append(check.hypothesis_id)
        return CheckOutcome(
            hypothesis_id=check.hypothesis_id, verdict="not-run", refusal_code="tool-unavailable"
        )

    field = [_hypothesis(f"h{index}", f"explanation {index}") for index in range(4)]
    call = CheckCall(tool="predict_pka", subject_note_id="compound-x")
    result = await _run_with(
        [run_check],
        _stubs(field=field, check_kind="computable", check_call=call, max_calculations=1),
        _request("q-tool-budget"),
    )

    assert len(ran_for) == 1, "more tool checks ran than the budget allows"
    codes = {row["outcome"]["refusal_code"] for row in result.data["ranked"]}
    assert "over-budget" in codes


async def test_the_requesters_roles_reach_the_activities_that_authorize() -> None:
    """The requester's roles reach the activities that authorize.

    Calc jobs are `expensive: true`, so an actor with no roles is refused under Entra, and the
    refusal would look like an ordinary grounding refusal.
    """
    seen: list[list[str]] = []

    @activity.defn(name="ground_check_job")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> ht._GroundedJob:
        seen.append(list(requested_roles or []))
        return _grounded(refusal_code="job-unavailable", refusal_detail="not served here")

    field = [_hypothesis("a", "the first explanation")]
    call = CheckCall(job="compare_solvents", subjects={"reactants": ["compound-x"]})
    request = TournamentRequest(
        question="q-roles",
        requested_by="chemist@example.com",
        requested_roles=["Chem.Privileged"],
    )
    await _run_with(
        [ground],
        _stubs(field=field, check_kind="computable", check_call=call),
        request,
    )

    assert seen == [["Chem.Privileged"]]


#: What the stand-in `TemplateWorkflow` was asked to run. Module level because Temporal refuses a
#: workflow class defined inside a function ("Local classes unsupported"), so the assertion cannot
#: close over a local list.
_TEMPLATE_RUNS: list[TemplateRunInput] = []


@workflow.defn(name="TemplateWorkflow", sandboxed=False)
class _StubTemplateWorkflow:
    """Stands in for the real template run, so the launch itself is what is under test."""

    @workflow.run
    async def run(self, run: TemplateRunInput) -> TemplateRunResult:
        _TEMPLATE_RUNS.append(run)
        return TemplateRunResult(
            template="tautomer-resolution", steps={}, result="the 1H form dominates at 94%"
        )


async def test_a_template_check_runs_the_reviewed_procedure_and_reports_it() -> None:
    """A template check runs the reviewed procedure and reports it.

    `tautomer-resolution` enumerates tautomers and ranks them, a subject the corpus does not
    contain. The check names the template and a note; everything else is the template's own.
    """
    _TEMPLATE_RUNS.clear()

    @activity.defn(name="ground_check_template")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> ht._GroundedTemplate:
        return ht._GroundedTemplate(
            template=discovered()["tautomer-resolution"],
            inputs={"smiles": "CC(=O)CC(C)=O"},
            ran="tautomer-resolution(smiles=[[compound-x]]) — defaults: solvent",
        )

    field = [_hypothesis("a", "the minor tautomer is what reacts")]
    call = CheckCall(template="tautomer-resolution", subject_note_id="compound-x")
    result = await _run_with(
        [ground],
        _stubs(field=field, check_kind="computable", check_call=call),
        _request("q-template"),
        children=[_StubTemplateWorkflow],
    )

    assert len(_TEMPLATE_RUNS) == 1, "the template child workflow was never started"
    assert _TEMPLATE_RUNS[0].inputs == {"smiles": "CC(=O)CC(C)=O"}
    assert _TEMPLATE_RUNS[0].requested_by == "chemist@example.com"
    row = result.data["ranked"][0]
    assert row["outcome"]["verdict"] != "not-run"
    assert "the 1H form dominates at 94%" in row["outcome"]["detail"]
    # The disclosure rule, on the third half: what ran, over which note, and what defaulted.
    assert "tautomer-resolution(smiles=[[compound-x]])" in row["outcome"]["ran"]
    assert "defaults: solvent" in row["outcome"]["ran"]


async def test_a_template_check_that_cannot_be_grounded_is_reported_not_dropped() -> None:
    """A refusal is an outcome carrying its code, the same as on the other two halves."""

    @activity.defn(name="ground_check_template")
    async def ground(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> ht._GroundedTemplate:
        return ht._GroundedTemplate(
            refusal_code="template-unrunnable-here",
            refusal_detail="this deployment serves no `chem` connector",
        )

    field = [_hypothesis("a", "the minor tautomer is what reacts")]
    call = CheckCall(template="tautomer-resolution", subject_note_id="compound-x")
    result = await _run_with(
        [ground],
        _stubs(field=field, check_kind="computable", check_call=call),
        _request("q-template-refused"),
    )

    row = result.data["ranked"][0]
    assert row["outcome"]["verdict"] == "not-run"
    assert row["outcome"]["refusal_code"] == "template-unrunnable-here"


async def test_a_template_a_deployment_turned_off_is_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template a deployment turned off is not reachable.

    Grounding uses `enabled()`, the set `templates_enabled` allows and the `run_<template>`
    launchers (and so `authz.side_effecting_tools()`) are built from, not every YAML `discovered()`.
    """
    monkeypatch.setattr(settings, "templates_enabled", "hazard-briefing")
    plan = await ht.ground_check_template(
        DiscriminatingCheck(
            hypothesis_id="a",
            question="which tautomer dominates?",
            kind="computable",
            expectation="the 1H form",
            call=CheckCall(template="tautomer-resolution", subject_note_id="compound-x"),
        ),
        "chemist@example.com",
        [],
        "corr",
    )

    assert plan.refused
    assert plan.refusal_code == "template-unavailable"
    assert "tautomer-resolution" in plan.refusal_detail


def _grounding_corpus(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> DiscriminatingCheck:
    """A corpus holding one resolvable compound, and a template check pointing at it."""
    from chemclaw.templates import registry

    monkeypatch.setattr(settings, "note_repo_dir", tmp_path)
    settings.knowledge_path.mkdir(parents=True)
    (settings.knowledge_path / "compound-x.md").write_text(
        "---\nid: compound-x\ntype: compound\ncompound_smiles: CCO\n---\nEthanol.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "templates_enabled", "tautomer-resolution")
    # This deployment's connector set is not what is under test; the launcher's gate is.
    monkeypatch.setattr(registry, "unrunnable_reason", lambda _template: None)
    return DiscriminatingCheck(
        hypothesis_id="a",
        question="which tautomer dominates?",
        kind="computable",
        expectation="the 1H form",
        call=CheckCall(template="tautomer-resolution", subject_note_id="compound-x"),
    )


async def test_a_template_check_is_authorized_as_the_launcher_a_chat_turn_would_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A template check is authorized as the launcher a chat turn would call.

    An operator's gate on `run_<template>` binds a tournament as it binds chat, with an audit row.
    """
    check = _grounding_corpus(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "entra_required", True)
    monkeypatch.setattr(settings, "tool_role_gates", {"run_tautomer_resolution": ["calc-users"]})

    refused = await ht.ground_check_template(check, "chemist@example.com", [], "corr")
    assert refused.refused
    assert refused.refusal_code == "tool-not-authorized"
    assert refused.template is None, "a refused launcher must leave nothing to start"

    allowed = await ht.ground_check_template(check, "chemist@example.com", ["calc-users"], "corr")
    assert not allowed.refused, allowed.refusal_detail
    assert allowed.template is not None
    assert allowed.template.name == "tautomer-resolution"


async def test_a_failed_evidence_sweep_costs_its_evidence_and_not_the_run() -> None:
    """A failed evidence sweep costs its evidence and not the run.

    A Temporal-level failure (timeout, lost worker) arrives as `ActivityError`; the tournament ranks
    on prose rather than discarding paid-for angles and generation.
    """
    from temporalio.exceptions import ApplicationError

    @activity.defn(name="gather_hypothesis_evidence")
    async def gather(
        query: str, hypothesis_id: str = "", requested_by: str = "", correlation_id: str = ""
    ) -> ht._EvidencePack:
        raise ApplicationError("the share hung", non_retryable=True)

    field = [
        _hypothesis("thermal", "the impurity is thermal in origin"),
        _hypothesis("wet", "the solvent was wet"),
    ]
    result = await _run_with([gather], _stubs(field=field, prefer="wet"), _request("q-no-evidence"))

    assert [row["hypothesis"]["id"] for row in result.data["ranked"]][0] == "wet"


def _per_hypothesis_checks(calls: dict[str, CheckCall | None]) -> Any:
    """A `derive_check` stand-in that gives each hypothesis its own call."""

    @activity.defn(name="derive_check")
    async def derive_check(request: ht._CritiqueRequest) -> DiscriminatingCheck:
        return DiscriminatingCheck(
            hypothesis_id=request.hypothesis.id,
            question=f"run the control for {request.hypothesis.id}",
            kind="computable",
            call=calls[request.hypothesis.id],
            expectation="the effect disappears if the hypothesis holds",
        )

    return derive_check


async def test_a_check_that_names_nothing_does_not_spend_the_calculation_budget() -> None:
    """A `call=None` leader refuses `no-call` without taking a slot a runnable check needed."""
    ran_for: list[str] = []

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> CheckOutcome:
        ran_for.append(check.hypothesis_id)
        return CheckOutcome(hypothesis_id=check.hypothesis_id, verdict="inconclusive", detail="1")

    tool = CheckCall(tool="predict_pka", subject_note_id="compound-x")
    field = [
        _hypothesis("wet", "the solvent was wet"),
        _hypothesis("thermal", "the impurity is thermal in origin"),
        _hypothesis("base", "the base decomposed"),
    ]
    result = await _run_with(
        [run_check, _per_hypothesis_checks({"wet": None, "thermal": tool, "base": tool})],
        _stubs(field=field, prefer="wet", max_calculations=2),
        _request("q-no-call-budget"),
    )

    assert sorted(ran_for) == ["base", "thermal"]
    codes = {
        row["hypothesis"]["id"]: row["outcome"]["refusal_code"] for row in result.data["ranked"]
    }
    assert codes["wet"] == "no-call"
    assert "over-budget" not in codes.values()


async def test_a_tool_check_temporal_could_not_complete_is_reported_not_dropped() -> None:
    """Mirrors `_settle_jobs`: a dispatched check that failed has an outcome and a code."""
    from temporalio.exceptions import ApplicationError

    attempts: list[str] = []

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> CheckOutcome:
        attempts.append(check.hypothesis_id)
        raise ApplicationError("worker lost")  # retryable: the policy is what bounds it

    field = [_hypothesis("a", "the first explanation")]
    call = CheckCall(tool="predict_pka", subject_note_id="compound-x")
    result = await _run_with(
        [run_check],
        _stubs(field=field, check_kind="computable", check_call=call),
        _request("q-tool-lost"),
    )

    outcome = result.data["ranked"][0]["outcome"]
    assert outcome["refusal_code"] == "tool-failed"
    assert outcome["verdict"] == "inconclusive"
    assert attempts == ["a"], "a governed tool call was re-invoked, writing a second audit row"


async def test_a_tool_check_is_bounded_as_a_calculation_not_as_a_model_call() -> None:
    """A tool check is bounded as a calculation, not as a model call.

    `run_computable_check` gets `hypothesis_check_timeout_seconds`, since it runs one attempt of a
    possibly long calculation. Read off the activity's own `info()`, the option actually scheduled.
    """
    seen: list[timedelta | None] = []

    @activity.defn(name="run_computable_check")
    async def run_check(
        check: DiscriminatingCheck,
        requested_by: str = "",
        requested_roles: list[str] | None = None,
        correlation_id: str = "",
    ) -> CheckOutcome:
        seen.append(activity.info().start_to_close_timeout)
        return CheckOutcome(hypothesis_id=check.hypothesis_id, verdict="inconclusive")

    field = [_hypothesis("a", "the first explanation")]
    call = CheckCall(tool="predict_pka", subject_note_id="compound-x")
    await _run_with(
        [run_check],
        _stubs(field=field, check_kind="computable", check_call=call),
        _request("q-tool-bound"),
    )

    assert seen == [timedelta(seconds=settings.hypothesis_check_timeout_seconds)]


def test_the_check_bound_outlasts_the_connector_it_waits_on() -> None:
    """The check bound outlasts every wait inside it.

    Connectors open concurrently (`connector_open_timeout_seconds`), a tool call waits its
    connector's `request_timeout`, and a `calc` miss may take `calc_server_timeout_seconds`; a
    shorter bound would discard a result its connector was entitled to finish. Derived from the
    shipped manifests.
    """
    from chemclaw.connectors.registry import discovered, request_timeout_seconds

    open_bound = settings.connector_open_timeout_seconds
    slowest_call = max(
        request_timeout_seconds(manifest.endpoint)
        for _, manifest in discovered().values()
        if manifest.endpoint is not None
    )
    assert settings.hypothesis_check_timeout_seconds >= slowest_call + open_bound
    assert (
        settings.hypothesis_check_timeout_seconds
        >= settings.calc_server_timeout_seconds + open_bound
    )
    assert settings.hypothesis_check_timeout_seconds > settings.hypothesis_call_timeout_seconds


async def test_two_tournaments_kept_apart_do_not_share_a_proposal_note() -> None:
    """The proposal id carries the field note's scope, so run B cannot overwrite run A's note."""
    written: list[str] = []

    @activity.defn(name="record_hypothesis_proposal")
    async def record(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        written.append(note_id)
        return note_id

    field = [_hypothesis("a", "the first explanation")]
    for actor in ("alice@example.com", "bob@example.com"):
        await _run_with(
            [record],
            _stubs(field=field),
            TournamentRequest(question="q-shared-proposal", requested_by=actor),
        )

    assert len(written) == 2
    assert written[0] != written[1]


async def test_a_proposal_cites_only_notes_the_hypothesis_evidence_returned() -> None:
    """A cited id nobody retrieved is the model's recollection, and is not filed as an edge.

    The generator cites from the question's sweep and the hypothesis has its own; an id in
    either is evidence, and a well-formed id in neither must not become a `[[link]]` on the note.
    """
    bodies: list[str] = []

    @activity.defn(name="gather_hypothesis_evidence")
    async def gather(
        query: str, hypothesis_id: str = "", requested_by: str = "", correlation_id: str = ""
    ) -> ht._EvidencePack:
        seen = "playbook-own" if hypothesis_id else "playbook-question"
        return ht._EvidencePack(hypothesis_id=hypothesis_id, framed=[], note_ids=[seen])

    @activity.defn(name="record_hypothesis_proposal")
    async def record(
        body: str, note_id: str, tags: list[str], requested_by: str = "", correlation_id: str = ""
    ) -> str:
        bodies.append(body)
        return note_id

    cited = _hypothesis("a", "the first explanation").model_copy(
        update={"cited_note_ids": ["playbook-question", "playbook-own", "playbook-invented"]}
    )
    await _run_with([gather, record], _stubs(field=[cited]), _request("q-retrieved-cites"))

    assert len(bodies) == 1
    assert "[[playbook-question]]" in bodies[0]
    assert "[[playbook-own]]" in bodies[0]
    assert "playbook-invented" not in bodies[0]


async def test_a_model_authored_hypothesis_id_is_bounded_where_it_enters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id reaches child workflow ids, so its length and charset are bounded at generation."""

    async def structured(model: type[Any], prompt: str) -> Any:
        return ht._HypothesisBatch(
            hypotheses=[
                Hypothesis(id="x" * 5000, statement="a long one", refuted_if="it is not"),
                Hypothesis(id="///", statement="an unsafe one", refuted_if="it is not"),
            ]
        )

    monkeypatch.setattr(ht, "_structured", structured)
    batch = await ht.generate_hypotheses(
        ht._GenerateRequest(question="q", angle="a", wanted=2, requested_by="c@example.com")
    )

    ids = [h.id for h in batch.hypotheses]
    assert ids[0] == "x" * ht._MAX_HYPOTHESIS_ID
    assert ids[1] == f"h-{stable_hash(['an unsafe one'])}"
