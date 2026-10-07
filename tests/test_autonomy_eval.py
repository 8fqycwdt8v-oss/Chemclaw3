"""The autonomy metrics, and the claim that their transcripts are the real thing.

Plan quality, a plan-vs-single-shot comparison and a runaway rate, scored on scripted transcripts.
One test drives the real `run_turn` and asserts its events match the committed cases, so the
metrics do not score a fiction. The iteration cap emits `loop_cap_reached`, so `runaway_rate`
reads that signal instead of inferring from open todos (which a deferral also leaves).
"""

import asyncio
from typing import Any

import pytest
import yaml

import chemclaw.api.runner as runner
from chemclaw.agent.session import TurnSession
from chemclaw.api.events import Event
from chemclaw.core.config import settings
from chemclaw.evals.harness import load_eval_cases
from chemclaw.evals.metric import EvalCase, MetricError, registered_names
from chemclaw.evals.metrics import precision_recall_f1

_ANSWER = {"type": "answer", "text": "done", "unsupported_claims": [], "review_required": False}


def _case(**kwargs: Any) -> EvalCase:
    """An eval case with the boilerplate filled in, so a test shows only what it is about."""
    kwargs.setdefault("id", "t")
    kwargs.setdefault("metrics", ["plan_quality"])
    return EvalCase(**kwargs)


def _score(name: str, case: EvalCase) -> Any:
    """Resolve and run one registered metric — through the registry, as the harness does."""
    from chemclaw.evals.metric import get_metric

    return get_metric(name)(case)


def _drive(agent: Any, session_id: str) -> list[Event]:
    """Collect one real turn's events from the front-door runner."""

    async def _collect() -> list[Event]:
        session = TurnSession(session_id=session_id)
        return [event async for event in runner.run_turn(session, "go")]

    return asyncio.run(_collect())


def test_the_autonomy_metrics_are_registered() -> None:
    """Registration is an import side effect, so a module left out of `evals/__init__` is dead."""
    assert {"plan_quality", "runaway_rate", "plan_execute_utility"} <= set(registered_names())


def test_a_capped_loop_is_a_runaway_and_says_so_in_the_transcript() -> None:
    """A capped loop is a runaway and says so in the transcript.

    That the runner emits `loop_cap_reached` is pinned in `tests/test_langgraph_agent.py`; here,
    that the metric scores it.
    """
    capped = [
        {"type": "plan", "todos": ["[ ] never finished"]},
        {"type": "token", "text": "still working on it"},
        {"type": "error", "message": "reached its 25-iteration limit", "code": "loop_cap_reached"},
        _ANSWER,
    ]
    result = _score(
        "runaway_rate", _case(metrics=["runaway_rate"], output={"transcripts": [capped]})
    )
    assert result.value == 1.0
    assert result.passed is False
    assert "loop_cap_reached" in result.provenance


def test_a_cut_off_turn_counts_even_though_it_planned_nothing() -> None:
    """The other runaway class: exhaustion the front door reports, with no plan behind it.

    `turn_timeout` and `budget_exhausted` mark a stopped turn, which may never have planned.
    """
    cut_off = [
        {"type": "token", "text": "thinking"},
        {"type": "error", "message": "out of budget", "code": "budget_exhausted"},
    ]
    case = _case(metrics=["runaway_rate"], output={"transcripts": [cut_off]})
    result = _score("runaway_rate", case)
    assert result.value == 1.0
    assert "budget_exhausted" in result.provenance


def test_an_ordinary_failure_is_not_a_runaway() -> None:
    """A storage outage is not the agent looping, and conflating them would make the rate noise."""
    failed = [{"type": "error", "message": "postgres is down", "code": "storage_unavailable"}]
    case = _case(metrics=["runaway_rate"], output={"transcripts": [failed]})
    result = _score("runaway_rate", case)
    assert result.value == 0.0


def test_a_turn_with_no_plan_at_all_is_not_a_runaway() -> None:
    """Most turns never plan — a one-shot question is answered, not project-managed.

    Counting "no plan" as a runaway would make the rate a measure of how often the harness is on.
    """
    plain = [{"type": "token", "text": "hi"}, _ANSWER]
    case = _case(metrics=["runaway_rate"], output={"transcripts": [plain]})
    result = _score("runaway_rate", case)
    assert result.value == 0.0


def test_an_open_step_is_not_by_itself_a_runaway() -> None:
    """An open step is not by itself a runaway.

    A turn may defer to a durable job, ask the chemist, or plan beyond one turn; only the cap firing
    makes a runaway.
    """
    open_step = [{"type": "plan", "todos": ["[ ] a"]}, _ANSWER]
    result = _score(
        "runaway_rate", _case(metrics=["runaway_rate"], output={"transcripts": [open_step]})
    )
    assert result.value == 0.0


def test_the_rate_is_a_fraction_of_the_turns_it_was_given() -> None:
    """Three turns, one of them cut off — the denominator has to be the turn count."""
    finished = [{"type": "plan", "todos": ["[x] a"]}, _ANSWER]
    capped = [
        {"type": "plan", "todos": ["[ ] a"]},
        {"type": "error", "message": "iteration limit", "code": "loop_cap_reached"},
        _ANSWER,
    ]
    result = _score(
        "runaway_rate",
        _case(
            metrics=["runaway_rate"],
            output={"transcripts": [finished, capped, finished]},
        ),
    )
    assert result.value == pytest.approx(1 / 3)
    assert "1/3" in result.provenance


def test_plan_quality_scores_the_plan_the_turn_ended_with() -> None:
    """A plan is revised as work proceeds; the last state is the one that describes the turn."""
    transcript = [
        {"type": "plan", "todos": ["[ ] a"]},
        {"type": "plan", "todos": ["[x] a", "[x] b"]},
        _ANSWER,
    ]
    result = _score(
        "plan_quality",
        _case(output={"transcript": transcript}, reference={"expected_plan_steps": ["a", "b"]}),
    )
    assert result.value == 1.0


def test_plan_quality_ignores_the_checkbox_and_would_score_zero_without_stripping_it() -> None:
    """`PlanEvent.todos` are display strings, and the prefix is not part of the step's identity.

    Comparing `"[x] a"` against `"a"` would score a perfect plan 0.0.
    """
    result = _score(
        "plan_quality",
        _case(
            output={"transcript": [{"type": "plan", "todos": ["[x] a", "[ ] b"]}, _ANSWER]},
            reference={"expected_plan_steps": ["a", "b"]},
        ),
    )
    assert result.value == 1.0
    # The unstripped comparison is what the fix avoids, stated as arithmetic rather than as a claim.
    _, _, unstripped = precision_recall_f1({"[x] a", "[ ] b"}, {"a", "b"})
    assert unstripped == 0.0


def test_plan_quality_is_deliberately_blind_to_order() -> None:
    """Two orderings of the same work are usually both right; gating on one gates on a preference.

    Stated as a test because it is a decision, not an accident of reusing a set-based helper — a
    reader who assumes order matters would otherwise "fix" it.
    """
    forwards = _score(
        "plan_quality",
        _case(
            output={"transcript": [{"type": "plan", "todos": ["[x] a", "[x] b"]}, _ANSWER]},
            reference={"expected_plan_steps": ["a", "b"]},
        ),
    )
    backwards = _score(
        "plan_quality",
        _case(
            output={"transcript": [{"type": "plan", "todos": ["[x] b", "[x] a"]}, _ANSWER]},
            reference={"expected_plan_steps": ["a", "b"]},
        ),
    )
    assert forwards.value == backwards.value == 1.0


def test_a_missing_step_fails_the_gate_and_names_what_was_missed() -> None:
    """A gate nobody can see firing is not a gate; a failure with no name is not actionable."""
    result = _score(
        "plan_quality",
        _case(
            output={"transcript": [{"type": "plan", "todos": ["[x] a"]}, _ANSWER]},
            reference={"expected_plan_steps": ["a", "b", "c"]},
        ),
    )
    assert result.value == pytest.approx(0.5)
    assert result.passed is False
    assert "missing: b, c" in result.provenance


def test_a_transcript_naming_an_event_the_front_door_cannot_emit_is_rejected() -> None:
    """The closed `Event` union does real work here, which is why the transcript is parsed by it.

    A typo'd or invented `type:` read as loose dicts would simply contain no `PlanEvent` and score
    as "the signal was absent" — a healthy-looking number from a case that measures nothing.
    """
    with pytest.raises(MetricError, match="not a valid front-door transcript"):
        _score(
            "plan_quality",
            _case(
                output={"transcript": [{"type": "plan_update", "todos": ["[x] a"]}]},
                reference={"expected_plan_steps": ["a"]},
            ),
        )


def test_a_turn_that_never_planned_is_an_error_not_a_score_of_zero() -> None:
    """Refusing beats scoring: absent evidence and bad evidence must not share a number.

    0.0 means "planned badly"; a turn with no plan must raise instead.
    """
    with pytest.raises(MetricError, match="no PlanEvent"):
        _score(
            "plan_quality",
            _case(
                output={"transcript": [{"type": "token", "text": "hi"}, _ANSWER]},
                reference={"expected_plan_steps": ["a"]},
            ),
        )


def test_plan_execute_utility_scores_the_helped_share_not_the_net_delta() -> None:
    """One float has to be comparable across case sets, and `net_delta` is not.

    The helped share is bounded and unit-free; a task in other units would dominate a sum of deltas.
    The deltas stay in the provenance.
    """
    result = _score(
        "plan_execute_utility",
        _case(
            metrics=["plan_execute_utility"],
            output={
                "higher_is_better": True,
                "tasks": [
                    {"task_id": "a", "baseline": 1.0, "augmented": 2.0},
                    {"task_id": "b", "baseline": 1.0, "augmented": 2.0},
                    {"task_id": "c", "baseline": 1.0, "augmented": 0.0},
                    {"task_id": "d", "baseline": 1.0, "augmented": 1.0},
                ],
            },
        ),
    )
    assert result.value == pytest.approx(0.5)  # 2 of 4 helped
    assert result.passed is None  # a progress number, not a defect gate
    assert "net delta +1" in result.provenance


def test_the_direction_is_honoured_so_a_lower_is_better_metric_is_not_inverted() -> None:
    """Regret and error go down when they improve; scoring them as gains inverts the verdict."""
    tasks = [{"task_id": "a", "baseline": 5.0, "augmented": 2.0}]
    lower = _score(
        "plan_execute_utility",
        _case(
            metrics=["plan_execute_utility"],
            output={"higher_is_better": False, "tasks": tasks},
        ),
    )
    higher = _score(
        "plan_execute_utility",
        _case(
            metrics=["plan_execute_utility"],
            output={"higher_is_better": True, "tasks": tasks},
        ),
    )
    assert lower.value == 1.0 and higher.value == 0.0


def test_a_yaml_yes_is_not_a_token_baseline() -> None:
    """`bool` is a subclass of `int`, and YAML writes `yes` where a reader sees a word.

    So `baseline_tokens: yes` must be refused rather than read as a baseline of 1, as
    `metrics._scalar` already does.
    """
    assert yaml.safe_load("baseline_tokens: yes") == {"baseline_tokens": True}
    turns = [{"correlation_id": "cost-01", "input_tokens": 1000, "output_tokens": 100}]
    with pytest.raises(MetricError, match="positive number of billed tokens"):
        _score(
            "turn_cost_ratio",
            _case(
                metrics=["turn_cost_ratio"],
                output={"turns": turns},
                reference=yaml.safe_load("baseline_tokens: yes"),
            ),
        )
    # A real baseline still scores, so the guard is a refusal of non-numbers and not of the metric.
    scored = _score(
        "turn_cost_ratio",
        _case(
            metrics=["turn_cost_ratio"],
            output={"turns": turns},
            reference={"baseline_tokens": 1100},
        ),
    )
    assert scored.value == pytest.approx(1.0)


def test_the_shipped_autonomy_cases_load_and_score() -> None:
    """The committed cases are part of the versioned set, not fixtures living beside the tests."""
    cases = {case.id: case for case in load_eval_cases(settings.eval_case_dir)}
    shipped = {
        "autonomy-plan-quality",
        "autonomy-plan-quality-drops-a-step",
        "autonomy-runaway-rate",
        "autonomy-plan-execute-utility",
    }
    assert shipped <= set(cases)
    for case_id in shipped:
        case = cases[case_id]
        for name in case.metrics:
            assert _score(name, case).provenance
    # The demonstration case is declared as one, so its failure is not counted as a regression.
    assert cases["autonomy-plan-quality-drops-a-step"].expect_pass is False
    assert cases["autonomy-plan-quality"].expect_pass is True
