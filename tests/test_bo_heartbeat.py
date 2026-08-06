"""BO activities heartbeat, and their `execute_activity` calls declare a timeout for it (Conn-F2).

Before this, all four `workflow.execute_activity` calls in `BoCampaignWorkflow.run` carried a flat
`start_to_close_timeout` and no `heartbeat_timeout`, and none of `propose_initial`/`propose_next`/
`evaluate_candidates` ever called `activity.heartbeat` — the same silently-killed-and-retried shape
REV-3 already fixed for calc's CREST jobs (D-136): a worker that dies mid-round is only noticed at
the full `bo_activity_timeout_seconds` budget, burning the round's cost again on every retry.

`propose_initial`/`propose_next` wrap the BoFire fit/acquisition step in the shared
`chemclaw.durable.heartbeat.beating` timer (the same helper `connectors.calc`'s two CREST jobs use,
Rule of Three); `evaluate_candidates` heartbeats directly between candidates, since a batch has a
real unit boundary the timer would only obscure.
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

    Proven against a fast objective (no calculator involved) so the beats can only be coming from
    the explicit per-candidate call this fix adds — a timer-based wrapper would not produce one
    beat per candidate, it would produce zero for a batch this fast.
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

    `initial_candidates` is monkeypatched to a slow function (real BoFire sampling is fast, and
    the point under test is the wiring — that a stuck fit would be noticed — not the sampling
    itself). `bo_activity_heartbeat_timeout_seconds` is shrunk so the test costs milliseconds.
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
    """`BoCampaignWorkflow.run` passes `heartbeat_timeout` to every `execute_activity` call.

    Checked over the AST rather than by running the workflow: the property under test is a keyword
    argument at a call site, which a live (Temporal-server-requiring, offline-skipped) workflow
    test cannot see any more directly than a parse can, and the parse runs everywhere.
    """
    tree = ast.parse(inspect.getsource(workflows))
    run_method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )
    calls = [
        node
        for node in ast.walk(run_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_activity"
    ]
    # Seed, propose, evaluate, and the campaign-record write the durable path gained when it
    # stopped leaving `resume_campaign` with nothing to find.
    assert len(calls) == 5, f"expected 5 execute_activity calls, found {len(calls)}"
    for call in calls:
        heartbeat_kwarg = next((kw for kw in call.keywords if kw.arg == "heartbeat_timeout"), None)
        assert heartbeat_kwarg is not None, (
            f"execute_activity call at line {call.lineno} has no heartbeat_timeout"
        )
