"""F9-T3: the autonomy metrics, and the claim that their transcripts are the real thing.

The row asked for plan quality, a plan-vs-single-shot A/B and a runaway/abort rate, all computable
on a scripted transcript. Two things about that are easy to get wrong and are what these tests are
for:

- **A hand-written transcript can encode a shape the front door never emits.** Then every metric
  scores a fiction, reports healthy numbers, and gates nothing. So one test here drives the real
  `run_turn` with a fake agent and asserts the events it produces are exactly what the committed
  cases contain — that is the only assertion that makes the other ones mean anything.
- **The iteration cap used to emit no event, and the metric paid for it.** `AgentLoopMiddleware`
  stops at `harness_max_loop_iterations` and returns normally, so `runaway_rate` inferred a cap
  from residue — an answer sent with todos still open — and thereby scored a turn that correctly
  deferred to a durable job as a runaway, because `mark_awaiting_job` leaves exactly that residue.
  The cap is observable now (`chemclaw.agent.loop_cap` → `ErrorEvent(code="loop_cap_reached")`),
  so the metric reads the outcome instead of guessing at it. Both halves are pinned below: the
  deferral is not a runaway, and the explicit signal is.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import AgentSession

import chemclaw.api.runner as runner
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
        session = AgentSession(session_id=session_id)
        return [event async for event in runner.run_turn(session, "go")]

    return asyncio.run(_collect())


def test_the_autonomy_metrics_are_registered() -> None:
    """Registration is an import side effect, so a module left out of `evals/__init__` is dead."""
    assert {"plan_quality", "runaway_rate", "plan_execute_utility"} <= set(registered_names())


def test_a_capped_loop_is_a_runaway_and_says_so_in_the_transcript() -> None:
    """The signal that replaced the residue: the runner states the cap fired.

    `chemclaw.agent.loop_cap` observes the loop's last decision and `run_turn` emits
    `loop_cap_reached` for it — the third member of the exhaustion family. That the *runner* really
    emits it for a really capped MAF loop is pinned in `tests/test_harness_execution.py`; what is
    pinned here is that the metric scores it, which is the half an eval case can see.
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

    `turn_timeout` and `budget_exhausted` are the two codes the front door reports for a turn that
    was stopped rather than finished. A turn can burn its budget before emitting any plan at all, so
    this path must not depend on there being one.
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
    """The residue heuristic is gone, and its absence is the behaviour worth pinning.

    An open step at the end of a turn is ordinary: the agent deferred it to a durable job, or asked
    the chemist something, or planned further than one turn's worth of work. Only the guard firing
    makes it a runaway, and the guard now says so itself.
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

    This is the specific mutation the metric would otherwise die of: comparing `"[x] a"` against a
    reference of `"a"` intersects to nothing, so a perfect plan scores 0.0 and the gate fires on
    every healthy turn until someone writes the checkboxes into the references — where they would
    then flip as work completed.
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

    A plan-quality of 0.0 says "it planned badly". A turn with no plan did not plan at all, and
    reporting the two identically would let a harness that stopped emitting plans look like a
    quality regression a reviewer would go hunting for in the prompt.
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

    The two tasks below help by 1.0 on their own scale; a third measured in percent yield would
    dominate any sum of deltas and move the drift band for reasons having nothing to do with
    planning. The share is bounded and unit-free; the deltas stay in the provenance.
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
