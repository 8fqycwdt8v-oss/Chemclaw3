"""When an answer gets challenged, and what stops the argument.

Two properties carry this file. The **trigger** must distinguish a team from a solo turn without
consulting a model, because the whole point of `agent/challenge_gate.py` is that being reviewed is
not the answering agent's decision. And the **revise loop must terminate**: a panel and a model that
disagree can disagree forever, and an unbounded round trip between them is a turn that never ends —
which is why the always-corroborating case below is the regression guard rather than an edge case.

The hook is driven directly (`aafter_model`) rather than through a compiled graph. What a compiled
graph would add is the `can_jump_to` edge, and that is asserted separately: `agent/loop_cap.py`
records at length what it cost to have a hook that decided correctly and was wired to nothing, so
the declaration is checked as a fact about the middleware rather than trusted.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chemclaw.agent import challenge as challenge_module
from chemclaw.agent import challenge_gate as gate_module
from chemclaw.agent import verifier as verifier_module
from chemclaw.agent.challenge import (
    ChallengeBrief,
    ChallengeVerdict,
    begin_turn_review,
    current_turn_review,
    end_turn_review,
)
from chemclaw.agent.challenge_gate import build_challenge_gate
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.team import begin_delegation_tally, end_delegation_tally, running_specialist
from chemclaw.core.config import settings


@pytest.fixture(autouse=True)
def _discovered() -> None:
    """Register the shipped profiles, as `create_app` does at startup."""
    from chemclaw.agent.profile_discovery import load_profiles

    load_profiles()


@pytest.fixture(autouse=True)
def _panel_on(monkeypatch: Any) -> None:
    """Run these tests with the gate enabled and a deterministic panel size."""
    monkeypatch.setattr(settings, "challenge_enabled", True)
    monkeypatch.setattr(settings, "challenge_panel_size", 2)
    monkeypatch.setattr(settings, "challenge_quorum", 1)
    monkeypatch.setattr(settings, "challenge_max_attempts", 1)
    # Both existing checks off, so `review_required` is only what a test sets deliberately.
    monkeypatch.setattr(settings, "verifier_enabled", False)
    monkeypatch.setattr(settings, "answer_shape_gate_enabled", False)


def _answered(text: str = "the answer") -> list[Any]:
    """A finished turn: a question, a tool result, and a tool-call-free reply."""
    return [
        HumanMessage(content="what is the melting point?"),
        AIMessage(content="", tool_calls=[{"name": "find_notes", "args": {}, "id": "c1"}]),
        ToolMessage(content="note-1 says 130 C", tool_call_id="c1"),
        AIMessage(content=text),
    ]


def _stub_panel(monkeypatch: Any, *, corroborates: bool, angles: int = 2) -> dict[str, int]:
    """Replace drafting and the panel with scripted stand-ins; return a call counter."""
    calls = {"draft": 0, "panel": 0}

    async def draft(*_a: Any, **_k: Any) -> list[ChallengeBrief]:
        calls["draft"] += 1
        return [ChallengeBrief(angle=f"a{i}", brief="b") for i in range(angles)]

    async def panel(*_a: Any, **_k: Any) -> list[ChallengeVerdict]:
        calls["panel"] += 1
        return [
            ChallengeVerdict(corroborates=corroborates, rationale="stated problem", angle=f"a{i}")
            for i in range(angles)
        ]

    async def hold(*_a: Any, **_k: Any) -> str | None:
        return "answer-review-corr-1"

    monkeypatch.setattr(gate_module, "draft_briefs", draft)
    monkeypatch.setattr(gate_module, "run_panel", panel)
    monkeypatch.setattr(gate_module, "start_answer_review", hold)
    return calls


def _gate() -> Any:
    """The middleware under test, built for the unnarrowed agent."""
    return build_challenge_gate(get_profile(None))


def _run(gate: Any, messages: list[Any], *, delegations: int = 0, attempts: int = 0) -> Any:
    """Drive the hook once with a given delegation count, inside a turn's ambients."""

    async def _inner() -> Any:
        tally = begin_delegation_tally()
        review = begin_turn_review()
        try:
            for i in range(delegations):
                with running_specialist(f"helper-{i}"):
                    pass
            # `running_specialist` deliberately does not count — the work path does — so the tally
            # is advanced the way `_AttributedSpecialist` advances it.
            from chemclaw.agent.team import _count_delegation

            for _ in range(delegations):
                _count_delegation()
            return await gate.aafter_model(
                {"messages": messages, "challenge_attempts": attempts}, None
            )
        finally:
            end_turn_review(review)
            end_delegation_tally(tally)

    return asyncio.run(_inner())


# --- the branch is a real graph edge --------------------------------------------------------------


def test_the_gate_declares_the_jumps_it_makes() -> None:
    """`can_jump_to` names both targets, so the conditional edge exists in a compiled graph.

    `agent/loop_cap.py` measured what the alternative costs: without the declaration the hook runs,
    counts correctly, returns `{"jump_to": ...}` on every call — and the graph goes on looping,
    because the edge is *built from* this declaration. A unit test proves the decision; only the
    declaration connects it to anything.
    """
    hook = _gate().__class__.__dict__["aafter_model"]
    assert {"model", "end"} <= set(hook.__can_jump_to__)


# --- the trigger ----------------------------------------------------------------------------------


def test_a_team_is_challenged_even_when_nothing_flagged_the_answer(monkeypatch: Any) -> None:
    """Two helpers is a team, and a team is challenged whatever the confidence checks said.

    This is the case the verifier structurally cannot cover: each helper reported on its own piece,
    the supervisor stitched them together, and a contradiction between pieces is invisible to a
    check that scores one finished answer against one turn's evidence.
    """
    calls = _stub_panel(monkeypatch, corroborates=False)
    _run(_gate(), _answered(), delegations=2)
    assert calls["panel"] == 1


def test_a_solo_turn_that_nothing_flagged_is_not_challenged(monkeypatch: Any) -> None:
    """No delegation and no flag means no panel — and no model calls spent finding that out.

    The cost control that makes the gate affordable to leave on: the panel is on the answer's hot
    path, so a turn nothing is worried about must not pay for one.
    """
    calls = _stub_panel(monkeypatch, corroborates=False)
    _run(_gate(), _answered(), delegations=0)
    assert calls["draft"] == 0
    assert calls["panel"] == 0


def test_a_single_helper_is_challenged_only_when_the_answer_was_flagged(monkeypatch: Any) -> None:
    """One helper is not a team; it is challenged on the existing signal, not unconditionally."""
    calls = _stub_panel(monkeypatch, corroborates=False)
    _run(_gate(), _answered(), delegations=1)
    assert calls["panel"] == 0

    # Patched on `verifier`, which is where the scan lives and where `score_answer` calls it — the
    # gate no longer scores for itself, so patching the gate's own namespace would silently do
    # nothing and this test would pass by asserting the wrong thing.
    monkeypatch.setattr(settings, "answer_shape_gate_enabled", True)
    monkeypatch.setattr(
        verifier_module, "ungrounded_parameter_shapes", lambda *_a: ["flow rate: 1.0 mL/min"]
    )
    _run(_gate(), _answered("run at 1.0 mL/min"), delegations=1)
    assert calls["panel"] == 1


def test_a_mid_turn_message_is_never_challenged(monkeypatch: Any) -> None:
    """A reply carrying tool calls is a step, not an answer.

    Challenging one would put a panel on a half-formed thought and then jump the graph back to a
    model that was in the middle of its work.
    """
    calls = _stub_panel(monkeypatch, corroborates=True)
    mid = [
        HumanMessage(content="q"),
        AIMessage(
            content="let me look", tool_calls=[{"name": "find_notes", "args": {}, "id": "c"}]
        ),
    ]
    assert _run(_gate(), mid, delegations=5) is None
    assert calls["panel"] == 0


# --- what an upheld objection does ---------------------------------------------------------------


def test_an_upheld_objection_sends_the_critique_back_to_the_model(monkeypatch: Any) -> None:
    """The revision round jumps to `model` *and* appends the critique to the thread.

    Both halves are load-bearing. The jump alone would re-run the model over exactly the input that
    produced the answer under objection — and produce it again — so the critique has to enter the
    conversation, not sit in a state field the model never reads.
    """
    _stub_panel(monkeypatch, corroborates=True)
    result = _run(_gate(), _answered(), delegations=2, attempts=0)
    assert result["jump_to"] == "model"
    assert result["challenge_attempts"] == 1
    appended = result["messages"][0]
    assert isinstance(appended, HumanMessage)
    assert "stated problem" in appended.text


def test_the_revision_loop_stops_at_its_configured_bound(monkeypatch: Any) -> None:
    """With the budget spent, the answer goes out marked instead of round-tripping again.

    **The termination guard.** The panel here corroborates every time, which is exactly the case an
    unbounded loop would never escape: the model revises, the panel objects again, forever. At the
    bound the gate must stop arguing and surface the disagreement.
    """
    _stub_panel(monkeypatch, corroborates=True)
    result = _run(_gate(), _answered(), delegations=2, attempts=settings.challenge_max_attempts)
    assert result is None  # no jump: the turn ends
    review = _last_review(monkeypatch, delegations=2, attempts=settings.challenge_max_attempts)
    assert review is not None
    assert review.review_required is True
    assert review.challenged is True
    assert review.hold_id == "answer-review-corr-1"
    assert any("stated problem" in claim for claim in review.unsupported)


def _last_review(monkeypatch: Any, *, delegations: int, attempts: int) -> Any:
    """Re-run the gate and hand back the verdict it published, for assertions on the ambient."""

    async def _inner() -> Any:
        tally = begin_delegation_tally()
        review = begin_turn_review()
        try:
            from chemclaw.agent.team import _count_delegation

            for _ in range(delegations):
                _count_delegation()
            await _gate().aafter_model(
                {"messages": _answered(), "challenge_attempts": attempts}, None
            )
            return current_turn_review()
        finally:
            end_turn_review(review)
            end_delegation_tally(tally)

    return asyncio.run(_inner())


def test_a_panel_that_finds_nothing_leaves_the_answer_alone(monkeypatch: Any) -> None:
    """Looking and finding nothing is a result: no jump, no mark, no hold."""
    _stub_panel(monkeypatch, corroborates=False)
    assert _run(_gate(), _answered(), delegations=2) is None
    review = _last_review(monkeypatch, delegations=2, attempts=0)
    assert review is not None
    assert review.review_required is False
    assert review.challenged is False
    assert review.hold_id is None


def test_a_quorum_short_of_agreement_does_not_uphold(monkeypatch: Any) -> None:
    """One objection out of a panel of two does not act when two are required.

    A persona briefed to find fault will find fault; requiring agreement between independently
    briefed angles is what separates a defect from one challenger's enthusiasm.
    """
    monkeypatch.setattr(settings, "challenge_quorum", 2)

    async def draft(*_a: Any, **_k: Any) -> list[ChallengeBrief]:
        return [ChallengeBrief(angle="a0", brief="b"), ChallengeBrief(angle="a1", brief="b")]

    async def panel(*_a: Any, **_k: Any) -> list[ChallengeVerdict]:
        return [
            ChallengeVerdict(corroborates=True, rationale="problem", angle="a0"),
            ChallengeVerdict(corroborates=False, rationale="", angle="a1"),
        ]

    monkeypatch.setattr(gate_module, "draft_briefs", draft)
    monkeypatch.setattr(gate_module, "run_panel", panel)
    assert _run(_gate(), _answered(), delegations=2) is None


# --- the ambient the runner reads -----------------------------------------------------------------


def test_the_verdict_is_published_even_when_nothing_was_challenged(monkeypatch: Any) -> None:
    """A clean turn still publishes its score, so the runner never re-judges the same answer.

    The judge is a paid call. `api/runner_answer.build_answer_event` reads this instead of running
    its own pass, and a gate that published nothing on the quiet path would silently double the
    cost of every turn it was enabled for.
    """
    _stub_panel(monkeypatch, corroborates=False)
    review = _last_review(monkeypatch, delegations=0, attempts=0)
    assert review is not None


def test_the_ambient_is_absent_outside_a_turn() -> None:
    """No slot, no verdict — so the runner's ungated path stays the default off the graph.

    `None` is meaningful rather than merely empty: it is what tells `build_answer_event` to score
    the answer itself, which is what every deployment with the gate off has always done.
    """
    assert current_turn_review() is None
    assert challenge_module.current_turn_review() is None


def test_a_profile_narrowed_to_nothing_still_builds_a_gate() -> None:
    """The gate is built per agent, so an unusual profile must not break its construction."""
    assert build_challenge_gate(AgentProfile(name="x", tool_names=frozenset())) is not None


def test_a_panel_that_raises_cannot_kill_the_turn(monkeypatch: Any) -> None:
    """A failure assembling the panel costs the review, never the answer.

    **The regression guard for the defect that made this gate fatal.** Each panel member already
    contained its own failure, but *assembling* one did not: `run_panel` resolves a profile and
    asserts the attenuation invariant before any member is built, so a deployment whose profiles did
    not line up raised straight through this hook and destroyed an answer the chemist had already
    earned. `challenger_for` removed the cause; this asserts the class is gone, so the next thing
    that learns to raise in there cannot do the same.
    """

    async def boom(*_a: Any, **_k: Any) -> list[ChallengeBrief]:
        raise RuntimeError("profiles do not line up")

    monkeypatch.setattr(gate_module, "draft_briefs", boom)
    assert _run(_gate(), _answered(), delegations=2) is None
    review = _last_review(monkeypatch, delegations=2, attempts=0)
    assert review is not None, "the turn produced no verdict at all"
    assert review.challenged is False


def test_the_revision_round_briefs_the_panel_on_the_chemists_question(monkeypatch: Any) -> None:
    """The critique this gate appends must not become the question the next panel reviews.

    Measured before the fix: pass two briefed the panel with "A review panel examined your answer…"
    as the QUESTION, so every revision round was judged against the wrong problem — and the answer
    that finally shipped had been reviewed by a panel that never saw what was asked.
    """
    seen: list[str] = []

    async def draft(question: str, *_a: Any, **_k: Any) -> list[ChallengeBrief]:
        seen.append(question)
        return [ChallengeBrief(angle="a0", brief="b")]

    async def panel(*_a: Any, **_k: Any) -> list[ChallengeVerdict]:
        return [ChallengeVerdict(corroborates=False, rationale="", angle="a0")]

    monkeypatch.setattr(gate_module, "draft_briefs", draft)
    monkeypatch.setattr(gate_module, "run_panel", panel)

    revised = [
        *_answered(),
        HumanMessage(content=_feedback_text(), name=gate_module._CRITIQUE_MARKER),
        AIMessage(content="a revised answer"),
    ]
    _run(_gate(), revised, delegations=2, attempts=1)
    assert seen == ["what is the melting point?"]


def _feedback_text() -> str:
    """What the gate appends on a revision round, as it actually words it."""
    return gate_module._feedback(
        [ChallengeVerdict(corroborates=True, rationale="unsupported", angle="a0")]
    )
