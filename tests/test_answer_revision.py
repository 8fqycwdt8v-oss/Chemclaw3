"""A flagged answer goes back for another pass, in the same turn.

ADR: `D-2026-09-15-a-flagged-answer-that-goes-out-flagged-is-a-verdict-nobody-acted-on`.

`agent/verifier.py` has always *marked* an answer whose claims its evidence does not support, and
`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` states in as many words what
happened next: "Nothing routes a flagged answer back for another pass." These tests drive the loop
that does, through the real `run_turn` over a real compiled graph (`tests/fakes_turn`), because the
thing under test is the *runner* and a stand-in for the graph would prove only that the stand-in was
called.

**The test that matters most is the one where revising does not help.** That is the shape
D-2026-08-16 found `RubricMiddleware` lacking — its `_finalize_evaluation` rewrites the result to
`max_iterations_reached` and mutates no message, so a grader outage ships every answer ungraded with
a log line nothing reads. Here, running out of rounds must leave the answer exactly as it would have
been with the loop off: shipped, and still marked.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import chemclaw.agent.verifier as verifier_module
import chemclaw.api.runner as runner
from chemclaw.agent.session import TurnSession
from chemclaw.agent.verifier import ClaimCheck, VerificationResult
from chemclaw.api.events import AnswerEvent
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes_turn import Piece, ScriptedTurn

#: The sentence `_revision_message` opens with. Matched rather than restated in full, so a reworded
#: prompt does not silently stop these fakes from recognising a revision.
_SENT_BACK = "not supported by it"

_FLAGGED = "Yield was 90% [[reaction-a]]."
_GROUNDED = "The evidence does not settle the yield [[reaction-a]]."


class _RevisingAgent(ScriptedTurn):
    """Answers with an unsupported claim, and answers differently when sent back."""

    def __init__(self) -> None:
        """Count the runs, so a test can assert how many times the graph was driven."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The flagged answer first; the grounded one on any revision pass."""
        self.runs += 1
        yield _GROUNDED if _SENT_BACK in message else _FLAGGED


class _StubbornAgent(ScriptedTurn):
    """Answers the same unsupported way however many times it is sent back."""

    def __init__(self) -> None:
        """Count the runs, so exhaustion can be told from a loop that never ran."""
        self.runs = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """The same flagged prose every time."""
        self.runs += 1
        yield _FLAGGED


def _grades_by_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier that flags the unsupported answer and passes the grounded one.

    Keyed on the answer's own text rather than on a call counter, so it grades what it is given —
    a counter would pass the second call whatever the model said, which is the assertion inverted.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)
    monkeypatch.setattr(settings, "verifier_confidence_threshold", 0.7)

    async def _verify(answer: str, *_: Any, **__: Any) -> VerificationResult:
        if answer.strip() == _GROUNDED:
            return VerificationResult(claims=[], confidence=1.0, verified_by="judge")
        return VerificationResult(
            claims=[ClaimCheck(text="Yield was 90%", supported=False)],
            confidence=0.2,
            verified_by="judge",
        )

    monkeypatch.setattr(verifier_module, "verify_turn_answer", _verify)


def _drive(agent: ScriptedTurn) -> list[Any]:
    """One turn's events, with no connectors."""

    async def _collect() -> list[Any]:
        session = TurnSession(session_id="s-revise")
        return [
            event
            async for event in runner.run_turn(
                session, "what was the yield?", connectors=[], graph_factory=agent.graph_factory
            )
        ]

    return asyncio.run(_collect())


def _answer(events: list[Any]) -> AnswerEvent:
    """The turn's answer event."""
    return next(e for e in events if isinstance(e, AnswerEvent))


def test_a_flagged_answer_is_sent_back_and_the_revision_is_what_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the answer a chemist receives is the grounded one.

    And it is the revision *alone*. `ledger.answer_text` joins the accumulated parts, so without
    clearing them the answer would be the flagged prose with the corrected prose stapled to its end
    — which is worse than either, and is what the mid-turn resume does on purpose because a resume
    continues an answer where a revision replaces one.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _RevisingAgent()

    answer = _answer(_drive(agent))

    assert answer.text == _GROUNDED
    assert _FLAGGED not in answer.text, "the revision was appended to the flagged answer"
    assert answer.review_required is False
    assert agent.runs == 2, "the graph was not driven a second time"


def test_a_revision_that_does_not_help_still_answers_and_stays_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhaustion is the case that must not silently ship, and must not swallow the turn either.

    A deployment that runs out of rounds ends up exactly where it would have been with the loop off:
    the answer goes out, carrying `review_required`. Nothing is lost that was not already lost — the
    second model call simply bought nothing, which is what the counter is for.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_review_exhausted_total")
    answer = _answer(_drive(agent))

    assert answer.text == _FLAGGED, "the turn must still answer"
    assert answer.review_required is True, "an exhausted loop must not clear the verdict"
    assert agent.runs == 3, "one original pass plus two revisions"
    assert METRICS.value("chemclaw_answer_review_exhausted_total") == before + 1


def test_the_loop_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 0 rounds the turn is byte-for-byte what it was before this loop existed.

    The shipped configuration, so this is the arm that says the feature costs nothing until a
    deployment asks for it.
    """
    _grades_by_text(monkeypatch)
    assert settings.answer_review_max_rounds == 0, "the loop must ship off"
    agent = _StubbornAgent()

    before = METRICS.value("chemclaw_answer_revisions_total")
    answer = _answer(_drive(agent))

    assert answer.text == _FLAGGED
    assert answer.review_required is True
    assert agent.runs == 1, "no revision may run with the loop off"
    assert METRICS.value("chemclaw_answer_revisions_total") == before


def test_a_grounded_answer_is_never_sent_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop triggers on the verdict, not on the setting being on.

    Without this, a revision loop that re-ran unconditionally would pass every other test here and
    double the model spend of every turn in the deployment.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 2)

    class _GoodAgent(ScriptedTurn):
        """Answers in a way the verifier accepts first time."""

        def __init__(self) -> None:
            self.runs = 0

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            self.runs += 1
            yield _GROUNDED

    agent = _GoodAgent()
    answer = _answer(_drive(agent))

    assert answer.review_required is False
    assert agent.runs == 1, "a grounded answer was sent back anyway"


def test_the_revision_names_the_claims_rather_than_saying_try_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision prompt with no specifics licenses rewording instead of regrounding.

    Asserted on the message the graph is actually driven with, captured off the fake, because the
    text is the whole mechanism: `_revision_message` naming nothing would leave every other test
    here passing.
    """
    _grades_by_text(monkeypatch)
    monkeypatch.setattr(settings, "answer_review_max_rounds", 1)

    seen: list[str] = []

    class _CapturingAgent(ScriptedTurn):
        """Records every message it is driven with."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            seen.append(message)
            yield _GROUNDED if _SENT_BACK in message else _FLAGGED

    _drive(_CapturingAgent())

    assert len(seen) == 2, "the revision did not run"
    assert "Yield was 90%" in seen[1], "the revision must name the claim it is about"
    assert "<retrieved-note" in seen[1] or "unsupported-claims" in seen[1], (
        "the model's own prose is quoted back at it, so it must arrive framed as data"
    )
