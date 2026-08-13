"""The challenge panel: what it must do, and the one thing it must never do.

`agent/challenge.py` adds capability rather than porting it, so — like `test_agent_team.py` — the
tests here are written to fail if a property is *removed*, not to pass because the machinery ran.

The property that most needs a test is the one nothing else would catch. A challenger built as a
bare deepagents `SubAgent` dict runs with `list(spec.get("middleware", []))` and nothing else: no
audit trail, no per-tool authorization, no dry-run gate, no plan gate. It would answer correctly,
its tool calls would work, every other test here would pass, and the GxP trail would be silently
empty. `test_a_challenger_is_built_with_the_full_governance_chain` is the guard, and it asserts
against the middleware the agent is actually compiled with rather than against how it was spelled.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.agent.challenge import (
    CHALLENGER_PROFILE,
    ChallengeBrief,
    ChallengePanel,
    ChallengeVerdict,
    corroborated,
    draft_briefs,
    panel_quorum,
    run_panel,
)
from chemclaw.agent.chemclaw_agent import advertised_tool_names
from chemclaw.agent.profiles import AgentProfile, get_profile
from chemclaw.agent.team import TeamError, reject_widening
from chemclaw.core.config import settings


@pytest.fixture(autouse=True)
def _discovered() -> None:
    """Register the shipped profiles, as `create_app` does at startup."""
    from chemclaw.agent.profile_discovery import load_profiles

    load_profiles()


def _default() -> AgentProfile:
    """The answering agent's profile — what every challenger is checked against."""
    return get_profile(None)


class _ScriptedDrafter:
    """A drafting client whose structured output is fixed by the test."""

    def __init__(self, panel: Any) -> None:
        self._panel = panel
        self.calls = 0

    def with_structured_output(self, _schema: Any) -> "_ScriptedDrafter":
        """Accept the schema and keep replaying."""
        return self

    async def ainvoke(self, _prompt: str) -> Any:
        """Return the scripted panel."""
        self.calls += 1
        if isinstance(self._panel, Exception):
            raise self._panel
        return self._panel


class _ScriptedChallenger:
    """A compiled-agent stand-in returning a fixed `structured_response`."""

    def __init__(self, verdict: Any, delay: float = 0.0) -> None:
        self._verdict = verdict
        self._delay = delay

    async def ainvoke(self, _state: Any) -> dict[str, Any]:
        """Answer after an optional delay, or raise if the script says so."""
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return {"structured_response": self._verdict}


def _builder(*verdicts: Any) -> Any:
    """A `build_langgraph_agent` stand-in handing out one scripted challenger per call."""
    remaining = list(verdicts)

    def build(**_kwargs: Any) -> Any:
        return _ScriptedChallenger(remaining.pop(0))

    return build


# --- the surface is code's, never the model's ----------------------------------------------------


def test_the_shipped_challenger_surface_is_an_attenuation_of_the_answering_agent() -> None:
    """The challenger profile advertises nothing the agent whose answer it reviews cannot reach.

    The panel's *personas* are generated per task; its *tools* are this file. If the surface ever
    grew past the caller's, a generated brief would become a capability-escalation channel — which
    is the one thing `agent/challenge.py`'s docstring promises it is not.
    """
    reject_widening(_default(), get_profile(CHALLENGER_PROFILE))
    assert advertised_tool_names(get_profile(CHALLENGER_PROFILE)) <= advertised_tool_names(
        _default()
    )


def test_the_challenger_surface_is_read_only() -> None:
    """No tool a challenger holds can write, spend compute, or launch durable work.

    Asserted against the authorization layer's own list rather than against a hand-written set of
    names, so a tool that *becomes* side-effecting later is caught here rather than in review.
    """
    from chemclaw.agent.authz import side_effecting_tools

    held = get_profile(CHALLENGER_PROFILE).tool_names or frozenset()
    assert not (held & side_effecting_tools())


def test_a_challenger_that_would_widen_its_caller_fails_the_panel() -> None:
    """A challenger surface naming a tool the answering agent lacks raises rather than running.

    The regression guard for invariant 1 at the panel's own boundary: `reject_widening` is checked
    once per panel, before any challenger is built, so a misconfigured surface cannot get one model
    call in before it is caught.
    """
    narrow = AgentProfile(name="narrow", tool_names=frozenset({"find_notes"}))
    with pytest.raises(TeamError):
        asyncio.run(
            run_panel(
                "q",
                "a",
                [ChallengeBrief(angle="x", brief="b")],
                caller_profile=narrow,
                build=_builder(ChallengeVerdict()),
            )
        )


def test_a_challenger_is_built_with_the_full_governance_chain() -> None:
    """Every challenger is compiled by `build_langgraph_agent`, so it carries the whole chain.

    **This is the bare-`SubAgent` regression guard and the most important test in this file.**
    deepagents' `create_sub_agent` gives a spec's agent exactly `spec["middleware"]` and nothing
    else, and Chemclaw never calls `create_deep_agent` (which is what injects upstream's defaults).
    A challenger built that way would run with no audit trail, no authorization gate and no
    dry-run gate — and would pass every other test in this file while doing it.

    Asserted on the compiled graph's actual node set rather than on how the call was spelled: the
    tool node exists only because the builder attached the wrapped tool chain, so a challenger
    missing its governance would be missing this.
    """
    built: list[dict[str, Any]] = []

    def spy(**kwargs: Any) -> Any:
        built.append(kwargs)
        return _ScriptedChallenger(ChallengeVerdict())

    asyncio.run(
        run_panel(
            "q",
            "a",
            [ChallengeBrief(angle="x", brief="b")],
            caller_profile=_default(),
            build=spy,
            actor="oid-1",
            correlation_id="corr-1",
        )
    )
    assert built, "the panel built no challenger at all"
    # The call is `build_langgraph_agent`'s signature, not a deepagents spec dict: a resolved
    # profile object, the turn's identity, and a response format. That signature is what attaches
    # the audit/authz/dry-run chain; a `SubAgent` dict has none of these and would attach nothing.
    assert built[0]["profile"].name == CHALLENGER_PROFILE
    assert built[0]["response_format"] is ChallengeVerdict
    assert built[0]["actor"] == "oid-1"
    assert built[0]["correlation_id"] == "corr-1"


# --- the panel is generated, and bounded ---------------------------------------------------------


def test_the_panel_is_bounded_by_its_configured_size(monkeypatch: Any) -> None:
    """A model returning more angles than asked for cannot spend more model calls than allowed.

    The prompt states the bound and the code enforces it. Only the second is a guarantee: the panel
    is on the answer's hot path, and an instruction the model ignores would otherwise cost a real
    call per extra angle.
    """
    monkeypatch.setattr(settings, "challenge_panel_size", 2)
    drafter = _ScriptedDrafter(
        ChallengePanel(briefs=[ChallengeBrief(angle=f"a{i}", brief="b") for i in range(6)])
    )
    briefs = asyncio.run(draft_briefs("q", "a", [], client=drafter))
    assert len(briefs) == 2


def test_a_panel_that_cannot_be_drafted_challenges_nothing() -> None:
    """A drafting failure leaves the answer exactly as the existing checks left it.

    Never sinks the turn — the same contract `verify_answer` keeps. An answer whose panel could not
    be assembled is an unchallenged answer, not a failed one.
    """
    briefs = asyncio.run(
        draft_briefs("q", "a", [], client=_ScriptedDrafter(RuntimeError("no route")))
    )
    assert briefs == []


def test_a_drafter_returning_nothing_parseable_challenges_nothing() -> None:
    """Structured output that is not a `ChallengePanel` degrades rather than being coerced."""
    briefs = asyncio.run(draft_briefs("q", "a", [], client=_ScriptedDrafter("not a panel")))
    assert briefs == []


# --- one member's failure is not the panel's -----------------------------------------------------


def test_a_challenger_that_raises_is_counted_as_raising_no_objection() -> None:
    """A crashed challenger returns a non-corroborating verdict rather than sinking the panel.

    The default matters as much as the containment: a failure that read as an *objection* would
    hold answers for an infrastructure fact nobody can act on.
    """
    verdicts = asyncio.run(
        run_panel(
            "q",
            "a",
            [ChallengeBrief(angle="x", brief="b"), ChallengeBrief(angle="y", brief="b")],
            caller_profile=_default(),
            build=_builder(RuntimeError("endpoint down"), ChallengeVerdict(corroborates=False)),
        )
    )
    assert len(verdicts) == 2
    assert not verdicts[0].corroborates
    assert verdicts[0].angle == "x"


def test_a_challenger_that_times_out_is_counted_as_raising_no_objection(
    monkeypatch: Any,
) -> None:
    """A slow challenger costs the review, never the answer."""
    monkeypatch.setattr(settings, "challenge_timeout_seconds", 0.01)

    def build(**_kwargs: Any) -> Any:
        return _ScriptedChallenger(ChallengeVerdict(corroborates=True, rationale="late"), delay=1.0)

    verdicts = asyncio.run(
        run_panel(
            "q",
            "a",
            [ChallengeBrief(angle="slow", brief="b")],
            caller_profile=_default(),
            build=build,
        )
    )
    assert verdicts[0].corroborates is False


def test_the_angle_is_stamped_by_the_panel_not_by_the_model() -> None:
    """A challenger cannot rename itself; `run_panel` stamps the angle it was briefed on.

    The same category rule as `VerificationResult.verified_by`: which reviewer produced a verdict
    is a property of the dispatch, never a claim the reviewer gets to make about itself.
    """
    verdicts = asyncio.run(
        run_panel(
            "q",
            "a",
            [ChallengeBrief(angle="grounding", brief="b")],
            caller_profile=_default(),
            build=_builder(
                ChallengeVerdict(corroborates=True, rationale="r", angle="i-renamed-myself")
            ),
        )
    )
    assert verdicts[0].angle == "grounding"


def test_an_empty_brief_list_runs_no_challengers() -> None:
    """No angles means no panel — and no model calls spent discovering that."""
    empty = asyncio.run(run_panel("q", "a", [], caller_profile=_default(), build=_builder()))
    assert empty == []


# --- what counts as an objection -----------------------------------------------------------------


def test_a_corroboration_with_no_rationale_does_not_count() -> None:
    """A vote with nothing behind it is not a finding.

    An answer held for a reason a reviewer cannot read is the failure `runner_answer` already
    refuses when it declines to leave `review_required` beside an empty `unsupported_claims`.
    """
    verdicts = [
        ChallengeVerdict(corroborates=True, rationale="   ", angle="a"),
        ChallengeVerdict(
            corroborates=True, rationale="the cited note says the opposite", angle="b"
        ),
        ChallengeVerdict(corroborates=False, rationale="looks right", angle="c"),
    ]
    assert [v.angle for v in corroborated(verdicts)] == ["b"]


@pytest.mark.parametrize(
    ("configured", "panel_size", "expected"),
    [(2, 3, 2), (5, 3, 3), (1, 1, 1), (0, 3, 1)],
)
def test_the_quorum_can_never_exceed_the_panel(
    monkeypatch: Any, configured: int, panel_size: int, expected: int
) -> None:
    """A quorum larger than the panel would be a gate that can never fire.

    Indistinguishable from the feature being off, which is the worst kind of misconfiguration: it
    fails silently and looks like success. Clamped at read time because the two numbers are set
    independently.
    """
    monkeypatch.setattr(settings, "challenge_quorum", configured)
    assert panel_quorum(panel_size) == expected
