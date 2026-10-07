"""BO activities heartbeat, and their `execute_activity` calls declare a timeout for it.

Without a heartbeat, a worker that dies mid-round is noticed only at the full
`bo_activity_timeout_seconds`, and each retry re-burns the round. `propose_initial`/`propose_next`
wrap the BoFire step in the shared `chemclaw.durable.heartbeat.beating` timer;
`evaluate_candidates` heartbeats between candidates, a natural unit boundary.
"""

import ast
import asyncio
import inspect
import time

import pytest
from temporalio import activity

from chemclaw.connectors.bo import activities, workflows
from chemclaw.core.config import settings
from chemclaw.science.bo.benchmarks.reizman_suzuki import build_problem, load_dataset
from chemclaw.science.bo.engine import initial_candidates
from chemclaw.science.bo.problem import Candidate


@pytest.fixture(autouse=True)
def _capture_heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route `activity.heartbeat` to a list instead of raising outside a real activity context."""
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))
    return beats


def test_evaluate_candidates_heartbeats_once_per_candidate(_capture_heartbeats: list[str]) -> None:
    """A batch is the natural unit boundary here, so it beats directly between candidates.

    A fast objective, so the beats can only come from the explicit per-candidate call; a timer would
    produce none.
    """
    problem = build_problem(load_dataset())
    candidates = initial_candidates(problem, 3)  # valid params for the real registered objective
    asyncio.run(activities.evaluate_candidates("reizman_suzuki", candidates))
    assert len(_capture_heartbeats) == len(candidates)
    assert _capture_heartbeats == [f"evaluating candidate {i}/3" for i in (1, 2, 3)]


def test_propose_initial_heartbeats_through_the_shared_timer(
    monkeypatch: pytest.MonkeyPatch, _capture_heartbeats: list[str]
) -> None:
    """A slow BoFire fit still beats, via `chemclaw.durable.heartbeat.beating`.

    `initial_candidates` is replaced by a slow function (the wiring is under test, not sampling),
    and the heartbeat timeout is shrunk so the test costs milliseconds.
    """
    monkeypatch.setattr(settings, "bo_activity_heartbeat_timeout_seconds", 4.0)  # -> 1s interval

    def _slow_initial_candidates(problem: object, n: int, seed: int | None) -> list[Candidate]:
        time.sleep(1.3)
        return [Candidate(params={"x": 0.0})]

    monkeypatch.setattr(activities, "initial_candidates", _slow_initial_candidates)
    problem = build_problem(load_dataset())

    result = asyncio.run(activities.propose_initial(problem, 1))
    assert len(result) == 1
    assert any("still running" in beat for beat in _capture_heartbeats), (
        f"a fit longer than bo_activity_heartbeat_timeout_seconds produced no beat: "
        f"{_capture_heartbeats}"
    )


def test_propose_next_heartbeats_through_the_shared_timer(
    monkeypatch: pytest.MonkeyPatch, _capture_heartbeats: list[str]
) -> None:
    """Same shared timer, exercised through `propose_next` instead of the seeding path."""
    monkeypatch.setattr(settings, "bo_activity_heartbeat_timeout_seconds", 4.0)  # -> 1s interval

    def _slow_propose_candidates(
        problem: object, observations: list[object], n: int, seed: int | None
    ) -> list[Candidate]:
        time.sleep(1.3)
        return [Candidate(params={"x": 0.0})]

    monkeypatch.setattr(activities, "propose_candidates", _slow_propose_candidates)
    problem = build_problem(load_dataset())

    result = asyncio.run(activities.propose_next(problem, [], 1))
    assert len(result) == 1
    assert any("still running" in beat for beat in _capture_heartbeats), (
        f"a fit longer than bo_activity_heartbeat_timeout_seconds produced no beat: "
        f"{_capture_heartbeats}"
    )


def test_every_bo_activity_call_declares_a_heartbeat_timeout() -> None:
    """`BoCampaignWorkflow` passes `heartbeat_timeout` to every `execute_activity` call.

    Checked over the AST, since the property is a keyword argument at a call site. Walked over the
    whole class rather than `run`, because helpers such as `_evaluate` are where calls move; the
    count is a floor and the keyword is the rule.
    """
    tree = ast.parse(inspect.getsource(workflows))
    campaign = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "BoCampaignWorkflow"
    )
    calls = [
        node
        for node in ast.walk(campaign)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_activity"
    ]
    # Seed propose, loop propose, the one shared evaluate in `_evaluate`, and two campaign-record
    # writes (per round and terminal); the per-round write keeps a killed campaign resumable.
    assert len(calls) == 5, f"expected 5 execute_activity calls, found {len(calls)}"
    for call in calls:
        heartbeat_kwarg = next((kw for kw in call.keywords if kw.arg == "heartbeat_timeout"), None)
        assert heartbeat_kwarg is not None, (
            f"execute_activity call at line {call.lineno} has no heartbeat_timeout"
        )
