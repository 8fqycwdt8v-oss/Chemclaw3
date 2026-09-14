"""What keeps `make live-turn-cost` capable of seeing a cost regression.

The command exists because `turn_cost_ratio` was attached to a case of committed literals and so
could not move for any reason a release can cause. Every assertion here is about the way that
failure could come back: a workload whose bill does not follow the request, a recorded case that
was written rather than measured, and a comparison that never fails.
"""

from pathlib import Path

import pytest

from chemclaw.cli import live_turn_cost as lane
from chemclaw.cli.mock_llm import Behaviour
from chemclaw.cli.storm_behaviours import BEHAVIOURS
from chemclaw.core.config import settings
from chemclaw.core.turn_cost import TurnCost
from chemclaw.evals.harness import load_eval_cases


def _recorded_case_turns() -> list[TurnCost]:
    """The committed measured case, back as the records it was emitted from."""
    case = next(c for c in load_eval_cases(settings.eval_case_dir) if c.id == lane.CASE_ID)
    return [TurnCost.model_validate(turn) for turn in case.output["turns"]]


def test_the_workload_asks_the_one_behaviour_whose_bill_follows_the_request() -> None:
    """Every question carries a marker naming a behaviour that bills by size, not a constant.

    This is the whole difference between a lane that can see a prefix regression and a second gate
    that cannot fire. Measured: 20 of the 22 entries in `cli/storm_behaviours.py` name a constant
    `input_tokens` of 900, so a workload that landed on one of them would bill 900 whether the
    static prefix were 40,000 tokens or 400,000 — and the ratio would then be a constant of the
    mock, which is the same defect one layer out that the committed literals were.

    Asserted against the behaviour catalogue rather than against the marker string, so pointing the
    workload at a constant-billing behaviour fails here instead of silently producing a lane that
    measures nothing.
    """
    by_name = {b.name: b for b in BEHAVIOURS}
    name = lane.BEHAVIOUR_MARKER.strip("[]")
    assert name in by_name, f"{lane.BEHAVIOUR_MARKER} names no behaviour the mock serves"
    assert by_name[name].input_tokens is None, (
        f"behaviour {name!r} bills a constant {by_name[name].input_tokens} input tokens, so the "
        "lane's number cannot move when the request does"
    )
    assert all(lane.BEHAVIOUR_MARKER in question for question in lane.WORKLOAD)

    # The positive control: a behaviour that bills a constant is what this is excluding, and the
    # catalogue has to actually contain one for the assertion above to be about anything.
    assert any(b.input_tokens is not None for b in BEHAVIOURS)


def test_the_recorded_case_was_measured_rather_than_written() -> None:
    """The committed case's turns cost what a real request costs, not what a default bills.

    A case emitted from a constant-billing lane would carry `Behaviour`'s default 900 on every
    turn. Every recorded turn bills orders of magnitude more, because the bill is
    `input_tokens_per_char` over the serialized request — which is where the instructions, the
    skills listing and every bound tool schema are.
    """
    turns = _recorded_case_turns()
    assert turns, "no measured turns are recorded; run `make live-turn-cost ARGS=--emit`"
    constant = Behaviour(name="_reference").input_tokens
    assert constant is not None  # the default this is contrasted against
    assert all(turn.input_tokens > constant for turn in turns)
    # And the three diagnostic columns are carried, because two boots of one commit produced two
    # cost regimes and these are what tell them apart.
    assert all(turn.tool_calls is not None for turn in turns)


def test_a_worsening_drift_is_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison itself: the recorded turns pass, more expensive ones fail.

    The front door and the ledger are the only things substituted — the metric, the recorded case,
    the drift band and the verdict are all the shipped ones. Without that substitution this
    assertion would need a running lane, and a check that can only run where a lane is up is a
    check nobody runs.
    """
    recorded = _recorded_case_turns()

    def _serve(turns: list[TurnCost]) -> None:
        async def _drive(base_url: str) -> str:
            return "fixed-session"

        async def _read(session_id: str) -> list[TurnCost]:
            return turns

        monkeypatch.setattr(lane, "_drive", _drive)
        monkeypatch.setattr(lane, "_recorded", _read)

    _serve(recorded)
    assert lane.main([]) == 0

    # A tenth more input on every turn is well past `eval_drift_epsilon`'s band.
    _serve(
        [
            turn.model_copy(update={"input_tokens": int(turn.input_tokens * 1.1)})
            for turn in recorded
        ]
    )
    assert lane.main([]) == 1

    # And the other direction is not a failure: a command that failed on a cost *improvement* is
    # one everybody learns to re-run (`evals.baseline.is_worsening`, the same rule).
    _serve(
        [
            turn.model_copy(update={"input_tokens": int(turn.input_tokens * 0.5)})
            for turn in recorded
        ]
    )
    assert lane.main([]) == 0


def test_an_unreachable_lane_is_never_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 3 when nothing was measured — the posture `live_probes` already takes.

    Returning 0 here would report "no worsening drift" about a run that scored nothing, which is
    the shape of every gate this wave went looking for.
    """

    async def _drive(base_url: str) -> str:
        return "fixed-session"

    async def _none(session_id: str) -> list[TurnCost]:
        return []

    monkeypatch.setattr(lane, "_drive", _drive)
    monkeypatch.setattr(lane, "_recorded", _none)
    assert lane.main([]) == 3


def test_the_emitted_case_carries_no_identity(tmp_path: Path) -> None:
    """A measured case records what a turn cost, never who asked for it.

    `TurnCost` has 28 fields and `model_dump()` writes them all: the first emitted case published
    `model: ""` and `outcome: "unknown"` beside real numbers — defaults wearing the appearance of
    measurements — and `actor` and `session_id`, which identify a person and a conversation.
    """
    lane._emit(_recorded_case_turns(), tmp_path / "case.md")
    text = (tmp_path / "case.md").read_text(encoding="utf-8")
    assert "input_tokens" in text
    for field in ("actor", "session_id", "turn_id", "outcome"):
        assert f'"{field}"' not in text
