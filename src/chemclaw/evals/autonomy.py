"""Autonomy metrics over a scripted transcript — did the *harness* behave?

Plan quality, plan-vs-single-shot utility, runaway rate and turn cost, registered as eval metrics so
a prompt, skill or middleware change that regresses agent behaviour shows up in `make eval` and
`baseline.json`.

The model's replies are scripted, so these test the harness, not the model's judgment: that a plan
is emitted and survives into the event stream, that a turn a guard cut off says so, that the A/B
arithmetic holds. Transcripts are validated against the closed `Event` union, so a case naming an
event type the front door cannot emit is rejected at load.
"""

from typing import Any

from pydantic import TypeAdapter, ValidationError

from chemclaw.api.events import ErrorEvent, Event, PlanEvent
from chemclaw.core.config import settings
from chemclaw.core.turn_cost import TurnCost
from chemclaw.evals.ab import TaskScores, compare_tool_utility
from chemclaw.evals.metric import Direction, EvalCase, MetricError, MetricResult, metric
from chemclaw.evals.metrics import precision_recall_f1

# Error codes meaning the turn was cut off rather than finished: out of wall clock, budget or loop
# iterations. Other failures (e.g. a storage outage) are not runaways.
_EXHAUSTION_CODES = frozenset(
    {"turn_timeout", "budget_exhausted", "loop_cap_reached", "spend_cap_reached"}
)

_TRANSCRIPT = TypeAdapter(list[Event])


def _transcript(raw: Any, field: str) -> list[Event]:
    """Parse one serialized transcript, naming the field when it is not one.

    Validation matters: an event `type:` the front door never emits would otherwise score as an
    absent signal.
    """
    if not isinstance(raw, list) or not raw:
        raise MetricError(f"{field} must be a non-empty list of front-door events")
    try:
        return _TRANSCRIPT.validate_python(raw)
    except ValidationError as exc:
        raise MetricError(f"{field} is not a valid front-door transcript: {exc}") from exc


def _final_plan(transcript: list[Event]) -> PlanEvent | None:
    """The last plan the turn emitted, which is the plan it finished with.

    `run_turn` emits a `PlanEvent` only when the plan changes, so the last one is the final state.
    """
    plans = [event for event in transcript if isinstance(event, PlanEvent)]
    return plans[-1] if plans else None


def _plan_steps(plan: PlanEvent) -> list[str]:
    """Every work item of a rendered plan, checkbox stripped, order preserved."""
    return [step[4:] if step[:4] in ("[ ] ", "[x] ") else step for step in plan.todos]


def billed_tokens(turn: TurnCost) -> float:
    """One turn's cost in input-token equivalents.

    Input and output at face value, the two cache counters at their configured weights (a cached
    read is charged at a fraction, a cache write at a premium). Public because
    `evals/delegation_run.billed_by_session` needs the same arithmetic.
    """
    return (
        turn.input_tokens
        + turn.output_tokens
        + turn.cache_read_tokens * settings.eval_cache_read_weight
        + turn.cache_write_tokens * settings.eval_cache_write_weight
    )


@metric("plan_quality", Direction.HIGHER_IS_BETTER, gated=True)
def plan_quality(case: EvalCase) -> MetricResult:
    """F1 of the plan the turn ended with against the steps the case says it needed.

    Reads `output.transcript` and `reference.expected_plan_steps`, scored with the shared
    `precision_recall_f1`. Order is deliberately not scored: two orderings of the same steps are
    usually both correct. Gated at `eval_plan_quality_min`, below 1.0 because an extra defensible
    step is not a regression.
    """
    if case.reference is None:
        raise MetricError("plan_quality needs a reference with `expected_plan_steps`")
    expected_raw = case.reference.get("expected_plan_steps")
    if not isinstance(expected_raw, (list, tuple)) or not expected_raw:
        raise MetricError("reference.expected_plan_steps must name at least one step")
    expected = {str(step) for step in expected_raw}

    plan = _final_plan(_transcript(case.output.get("transcript"), "output.transcript"))
    if plan is None:
        raise MetricError(
            "output.transcript emitted no PlanEvent, so there is no plan to score; a turn that "
            "never planned is a harness failure to assert elsewhere, not a plan of quality zero"
        )
    produced = set(_plan_steps(plan))
    precision, recall, f1 = precision_recall_f1(produced, expected)
    missing = sorted(expected - produced)
    spurious = sorted(produced - expected)
    return MetricResult(
        metric="plan_quality",
        value=f1,
        unit=None,
        passed=f1 >= settings.eval_plan_quality_min,
        provenance=(
            f"precision {precision:.3f}, recall {recall:.3f} over {len(expected)} expected step(s)"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(spurious)}" if spurious else "")
        ),
    )


@metric("runaway_rate", Direction.LOWER_IS_BETTER, gated=True)
def runaway_rate(case: EvalCase) -> MetricResult:
    """Share of the case's turns that a guard cut off instead of letting them finish.

    Reads `output.transcripts` (a list, since a rate over one turn is meaningless). A turn is a
    runaway when it carries an `ErrorEvent` with a code in `_EXHAUSTION_CODES`: `turn_timeout`,
    `budget_exhausted`, `loop_cap_reached` or `spend_cap_reached`. The transcript states the
    outcome; the metric does not infer it from unchecked plan steps, which a correctly deferred step
    also leaves. Gated at `eval_runaway_max` (0.0): the scripted turns are meant to complete.
    """
    raw = case.output.get("transcripts")
    if not isinstance(raw, list) or not raw:
        raise MetricError("output.transcripts must be a non-empty list of transcripts")
    runaways: list[str] = []
    for index, one in enumerate(raw):
        transcript = _transcript(one, f"output.transcripts[{index}]")
        cut_off = [
            event
            for event in transcript
            if isinstance(event, ErrorEvent) and event.code in _EXHAUSTION_CODES
        ]
        if cut_off:
            runaways.append(f"#{index} cut off ({cut_off[-1].code})")
    value = len(runaways) / len(raw)
    return MetricResult(
        metric="runaway_rate",
        value=value,
        unit=None,
        passed=value <= settings.eval_runaway_max,
        provenance=(
            f"{len(runaways)}/{len(raw)} turn(s) were cut off before they finished"
            + (f"; {'; '.join(runaways)}" if runaways else "")
        ),
    )


@metric("plan_execute_utility", Direction.HIGHER_IS_BETTER)
def plan_execute_utility(case: EvalCase) -> MetricResult:
    """Share of tasks the planning path helped, against the single-shot baseline.

    Reads `output.tasks` (`task_id`, `baseline`, `augmented` per task) and
    `output.higher_is_better`, compared by `evals.ab.compare_tool_utility`. The scalar is the helped
    share, bounded in [0, 1], not `net_delta`, whose scale depends on the task; the signed deltas
    stay in the provenance. Ungated: it is a progress number, not a defect check.
    """
    raw = case.output.get("tasks")
    if not isinstance(raw, list) or not raw:
        raise MetricError("output.tasks must be a non-empty list of {task_id, baseline, augmented}")
    higher_is_better = case.output.get("higher_is_better")
    if not isinstance(higher_is_better, bool):
        raise MetricError("output.higher_is_better must be a bool — the metric's own direction")
    try:
        tasks = [TaskScores.model_validate(task) for task in raw]
    except ValidationError as exc:
        raise MetricError(f"output.tasks is not a list of task scores: {exc}") from exc
    summary = compare_tool_utility(tasks, higher_is_better=higher_is_better)
    value = len(summary.helped) / len(tasks)
    return MetricResult(
        metric="plan_execute_utility",
        value=value,
        unit=None,
        passed=None,
        provenance=(
            f"{len(summary.helped)}/{len(tasks)} task(s) helped, {len(summary.hurt)} hurt, "
            f"{len(summary.no_effect)} unchanged; net delta {summary.net_delta:+.4g} in the "
            f"{'higher' if higher_is_better else 'lower'}-is-better direction"
        ),
    )


@metric("turn_cost_ratio", Direction.LOWER_IS_BETTER)
def turn_cost_ratio(case: EvalCase) -> MetricResult:
    """What the case's turns cost, as a ratio against the same turns' recorded baseline.

    Reads `output.turns` (`TurnCost` records) and `reference.baseline_tokens`, so a suite can ask
    whether an answer is right for what it costs. A ratio rather than money: bounded, comparable
    across case sets, and moved only by changes to this system, not by provider prices. (The shipped
    case commits literal turn records, so its score is constant.)

    Billed tokens, not sent tokens: cache reads and writes are weighted, so a prompt-caching
    improvement is not scored as a regression. Turns that never answered are counted, and their
    number is in the provenance. Ungated until there is enough history to define a cost regression;
    the row in `baseline.json` is watched for drift.
    """
    raw = case.output.get("turns")
    if not isinstance(raw, list) or not raw:
        raise MetricError("output.turns must be a non-empty list of turn-cost records")
    try:
        turns = [TurnCost.model_validate(turn) for turn in raw]
    except ValidationError as exc:
        raise MetricError(f"output.turns is not a list of turn costs: {exc}") from exc

    baseline = (case.reference or {}).get("baseline_tokens")
    # Bools refused before the numeric test: YAML parses `yes` as a bool, and `bool` is an `int`.
    if isinstance(baseline, bool) or not isinstance(baseline, (int, float)) or baseline <= 0:
        raise MetricError("reference.baseline_tokens must be a positive number of billed tokens")

    billed = sum(billed_tokens(turn) for turn in turns)
    unfinished = sum(1 for turn in turns if not turn.completed)
    return MetricResult(
        metric="turn_cost_ratio",
        value=billed / float(baseline),
        unit=None,
        passed=None,
        provenance=(
            f"{billed:,.0f} billed token-equivalents over {len(turns)} turn(s) "
            f"({unfinished} of which never answered) against a baseline of {baseline:,.0f}; "
            f"cache reads weighted {settings.eval_cache_read_weight:g}x and writes "
            f"{settings.eval_cache_write_weight:g}x an input token"
        ),
    )
