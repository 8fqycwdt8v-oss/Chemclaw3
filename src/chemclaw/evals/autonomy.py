"""Autonomy metrics over a scripted transcript (F9-T3) — did the *harness* behave?

The ticket asks for plan quality, a plan-vs-single-shot A/B, and a runaway/abort rate. The backlog
row framed this as "zero evaluation of agent behaviour", which overstates it:
`tests/test_harness_execution.py` already drives real MAF machinery and pins the loop cap. What was
actually missing is that none of it reaches the **eval layer** — so a prompt edit, a skill change or
a middleware reorder could regress behaviour and `make eval` would say nothing, and no number
entered `baseline.json` for the drift check to watch.

**What these metrics do and do not measure, because the names invite the wrong reading.** A case
here carries a *scripted* transcript: the model's replies are pinned, so nothing about the model's
judgment is under test. What is under test is the harness around it — that a plan is emitted at
all, that its steps survive into the event stream, that work is closed before an answer goes out,
that the A/B arithmetic holds. **These do not supersede AG-13**, which is deferred on a live
endpoint precisely because judging judgment needs one. `retrieval_recall` once carried a name that
promised more than it scored, and the correction is cheaper written down than discovered.

**The transcript is validated against the closed `Event` union**, not read as loose dicts. That is
the union's whole value here: a case naming an event type the front door cannot emit is rejected at
load rather than scored as an absent signal.
"""

from typing import Any

from pydantic import TypeAdapter, ValidationError

from chemclaw.api.events import AnswerEvent, ErrorEvent, Event, PlanEvent
from chemclaw.core.config import settings
from chemclaw.evals.ab import TaskScores, compare_tool_utility
from chemclaw.evals.metric import EvalCase, MetricError, MetricResult, metric
from chemclaw.evals.metrics import precision_recall_f1

# Error codes that mean the turn was cut off rather than finished. `turn_timeout` and
# `budget_exhausted` are the two exhaustion outcomes the front door reports; the rest of the
# taxonomy describes failures that are not runaways (a storage outage is not the agent looping).
_EXHAUSTION_CODES = frozenset({"turn_timeout", "budget_exhausted"})

_TRANSCRIPT = TypeAdapter(list[Event])


def _transcript(raw: Any, field: str) -> list[Event]:
    """Parse one serialized transcript, naming the field when it is not one.

    Validation is the point rather than a formality: these cases are hand-written, and a `type:`
    the front door never emits would otherwise score as "the signal was absent" — which is exactly
    how a metric reports a healthy system while measuring nothing.
    """
    if not isinstance(raw, list) or not raw:
        raise MetricError(f"{field} must be a non-empty list of front-door events")
    try:
        return _TRANSCRIPT.validate_python(raw)
    except ValidationError as exc:
        raise MetricError(f"{field} is not a valid front-door transcript: {exc}") from exc


def _final_plan(transcript: list[Event]) -> PlanEvent | None:
    """The last plan the turn emitted, which is the plan it finished with.

    `run_turn` emits a `PlanEvent` only when the plan *changes*, so the last one is the final state
    rather than one sample of many.
    """
    plans = [event for event in transcript if isinstance(event, PlanEvent)]
    return plans[-1] if plans else None


def _open_steps(plan: PlanEvent) -> list[str]:
    """The still-unchecked work items of a rendered plan.

    `PlanEvent.todos` carries display strings — `"[x] title"` / `"[ ] title"` — because the surfaces
    must not have to infer completion state (`agent.harness_todo.todo_titles`). Reading the checkbox
    back off is the price of scoring the transcript the user actually saw rather than a second,
    private view of the same state.
    """
    return [step[4:] for step in plan.todos if step.startswith("[ ] ")]


def _plan_steps(plan: PlanEvent) -> list[str]:
    """Every work item of a rendered plan, checkbox stripped, order preserved."""
    return [step[4:] if step[:4] in ("[ ] ", "[x] ") else step for step in plan.todos]


@metric("plan_quality")
def plan_quality(case: EvalCase) -> MetricResult:
    """F1 of the plan the turn ended with against the steps the case says it needed.

    Reads `output.transcript` and `reference.expected_plan_steps`. Scored with the same
    `precision_recall_f1` the retrieval metrics use, so "did it name the right things" has one
    definition in this system rather than two.

    **Order is deliberately not scored, and that is a decision rather than an inheritance.** The
    shared computation is set-based, and for a plan that is the right call: two orderings of the
    same steps are usually both correct — run the calculation before or after pulling the ELN
    history — and penalising one would gate on a preference. What is genuinely wrong is naming a
    step that should not be there or dropping one that should, which is what precision and recall
    already say.

    The gate is `eval_plan_quality_min` (0.8), below 1.0 on purpose: an extra defensible step is
    not a regression, a missing required one is.
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


@metric("runaway_rate")
def runaway_rate(case: EvalCase) -> MetricResult:
    """Share of the case's turns that stopped without finishing what they had planned.

    Reads `output.transcripts` — a list of transcripts, because a rate over one turn is a coin
    flip and the name would be a lie. A turn counts as a runaway when either:

    - it ends in an `ErrorEvent` whose code is `turn_timeout` or `budget_exhausted`, the two ways
      the front door reports being cut off; or
    - it produced an `AnswerEvent` while its final plan still held unchecked steps.

    **The second clause exists because the iteration cap emits no event, and that is worth stating
    plainly.** `AgentLoopMiddleware` stops at `harness_max_loop_iterations` and returns normally, so
    a capped turn is externally identical to a finished one apart from its residue: open todos and
    an answer anyway. `tests/test_harness_execution.py` asserts exactly that residue
    (`assert not items[0].is_complete`) from inside the process; this is the same observation made
    from the transcript, which is all an eval case has. A metric that waited for a `runaway` event
    would score 0.0 forever and read as proof the cap never fires.

    Gated at `eval_runaway_max` (0.0): the pinned turns are scripted to complete, so a runaway
    among them is broken plumbing rather than a hard problem.
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
            continue
        plan = _final_plan(transcript)
        answered = any(isinstance(event, AnswerEvent) for event in transcript)
        if answered and plan is not None and _open_steps(plan):
            runaways.append(f"#{index} answered with {len(_open_steps(plan))} step(s) still open")
    value = len(runaways) / len(raw)
    return MetricResult(
        metric="runaway_rate",
        value=value,
        unit=None,
        passed=value <= settings.eval_runaway_max,
        provenance=(
            f"{len(runaways)}/{len(raw)} turn(s) did not finish their plan"
            + (f"; {'; '.join(runaways)}" if runaways else "")
        ),
    )


@metric("plan_execute_utility")
def plan_execute_utility(case: EvalCase) -> MetricResult:
    """Share of tasks the planning path helped, against the single-shot baseline.

    Reads `output.tasks` (`task_id`, `baseline`, `augmented` per task) and
    `output.higher_is_better`. The comparison itself is `evals.ab.compare_tool_utility`, which
    already implemented this A/B and was simply never registered as a metric — so it ran under no
    `make eval`, gated nothing, and put no number in `baseline.json`. Registering it is most of
    what this row needed.

    **The scalar is the helped *share*, not `net_delta`.** A metric's value is one float, and
    `net_delta` is unbounded and denominated in whatever the task's own scale happens to be, so a
    single task on a percent-yield scale would swamp four on a log-solubility scale and the drift
    band would be meaningless. The share is bounded in [0, 1] and comparable across case sets; the
    signed deltas stay in the provenance where a reader can see them.

    Ungated (`passed=None`): "how often does planning help" is a progress number, and a threshold
    on it would gate the eval suite on a research question rather than on a defect.
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
