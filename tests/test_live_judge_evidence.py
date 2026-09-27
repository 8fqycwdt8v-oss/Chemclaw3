"""What the live judge is shown of a tool result, and what it is told of a conditional capability.

Two defects from the 2026-09-27 run against a real model, both of them the judge grading against
less than the turn had:

- **pl-16** was judged "fabricated" for citing "Buckley et al., Org. Process Res. Dev. 2021, 25,
  587" and "Bretherick's Handbook, 8th ed." — both of which `screen_hazards` had returned, as the
  citations of its first and third flags, past character 200 of a result the model read whole. The
  judge saw the 200-character preview.
- **ws-12, rp-09, pl-22** were judged "fabricated" for offering to run code in a lane that could
  (`needs_bundle: pyexec`); the judge had the bucket and nothing saying whether the bundle was
  bound.
"""

import pytest

from chemclaw.core.config import settings
from chemclaw.evals.live import ProbeOutcome, ToolResult
from chemclaw.evals.live_judge import _prompt
from tests.test_live_probes import _probe, _run

# `screen_hazards`' shape for pl-16's combined species, the two citations the judge called invented
# placed where the real result had them: after the first flag's explanation.
_SCREEN = (
    '{"flags": [{"rule_id": "hydride-with-dipolar-aprotic", "severity": "high", "explanation": '
    '"Sodium hydride in DMF undergoes a documented autocatalytic exotherm above roughly 40 C; use '
    'THF or 2-MeTHF, or hold below that temperature with active cooling.", "citation": "Buckley et '
    'al., Org. Process Res. Dev. 2021, 25, 587 (NaH/DMF thermal runaway)"}, '
    '{"rule_id": "peroxide", '
    '"severity": "high", "explanation": "Peroxide linkage: energetic.", "citation": "Bretherick\'s '
    'Handbook of Reactive Chemical Hazards, 8th ed."}]}'
)


def test_a_citation_past_the_preview_reaches_the_judge_when_the_stream_carried_the_result() -> None:
    """Driven through `run_probe`: the inline result is kept, and the judge's prompt quotes it."""
    assert "Buckley" not in _SCREEN[:200], "the fixture must put the citation past the preview"
    outcome = _run(
        _probe(),
        {
            "type": "tool_result",
            "tool": "screen_hazards",
            "preview": _SCREEN[:200],
            "result_inline": _SCREEN,
        },
        {"type": "answer", "text": "Buckley et al., Org. Process Res. Dev. 2021, 25, 587."},
    )
    (result,) = outcome.tool_results
    assert result.text == _SCREEN
    prompt = _prompt(_probe(), outcome)
    assert "Buckley et al., Org. Process Res. Dev. 2021, 25, 587" in prompt
    assert "Bretherick's Handbook" in prompt


def test_the_judge_bound_is_the_setting_and_zero_restores_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded and config-driven: a cap cuts the text, and 0 shows the preview alone."""
    outcome = ProbeOutcome(
        probe_id="t-01",
        section=1,
        persona="lab_technician",
        bucket="A",
        question="q",
        tool_results=[ToolResult(tool="screen_hazards", preview="PREVIEW", text=_SCREEN)],
    )
    monkeypatch.setattr(settings, "live_probe_judge_result_chars", 250)
    bounded = _prompt(_probe(), outcome)
    assert _SCREEN[:250] in bounded and _SCREEN[:251] not in bounded
    monkeypatch.setattr(settings, "live_probe_judge_result_chars", 0)
    assert "[screen_hazards] PREVIEW" in _prompt(_probe(), outcome)


@pytest.mark.parametrize(("applied", "said"), [(True, "WAS bound"), (False, "was NOT bound")])
def test_a_conditional_probe_tells_the_judge_which_lane_it_ran_in(applied: bool, said: str) -> None:
    """`expected_tools_met` is set only when the expectation applied, so it names the lane."""
    probe = _probe(bucket="B", needs_bundle="pyexec", expects_tools=["run_python"])
    outcome = ProbeOutcome(
        probe_id="t-01",
        section=1,
        persona="lab_technician",
        bucket="B",
        question="q",
        expected_tools_met=False if applied else None,
    )
    prompt = _prompt(probe, outcome)
    assert f"run_python (bundle `pyexec`) {said}" in prompt


def test_an_unconditional_probe_carries_no_lane_line() -> None:
    """Only a `needs_bundle:` probe is graded in two lanes; nothing else gets the line."""
    outcome = ProbeOutcome(
        probe_id="t-01", section=1, persona="lab_technician", bucket="A", question="q"
    )
    assert "CONDITIONAL CAPABILITY" not in _prompt(_probe(), outcome)
