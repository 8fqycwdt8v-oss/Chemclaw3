"""What keeps `make live-turn-cost` capable of seeing a cost regression.

Each assertion guards a way the lane could stop measuring: a workload whose bill does not follow
the request, a recorded case written rather than measured, and a comparison that never fails.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from chemclaw.cli import live_turn_cost as lane
from chemclaw.cli.mock_llm import Behaviour, MockLlm
from chemclaw.cli.storm_behaviours import BEHAVIOURS
from chemclaw.core.config import settings
from chemclaw.core.turn_cost import TurnCost
from chemclaw.evals.harness import load_eval_cases
from chemclaw.evals.metric import EvalCase


@pytest.fixture(autouse=True)
def _no_gateway_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reads a real mock gateway's stats; one that needs a surface sets its own."""

    async def _none() -> None:
        return None

    monkeypatch.setattr(lane, "_mock_surface", _none)


def _recorded_case_turns() -> list[TurnCost]:
    """The committed measured case, back as the records it was emitted from."""
    case = next(c for c in load_eval_cases(settings.eval_case_dir) if c.id == lane.CASE_ID)
    return [TurnCost.model_validate(turn) for turn in case.output["turns"]]


def test_the_workload_asks_the_one_behaviour_whose_bill_follows_the_request() -> None:
    """Every question names a behaviour that bills by request size, not a constant.

    Most behaviours in `cli/storm_behaviours.py` bill a constant `input_tokens`, which would make
    the ratio a constant of the mock. Asserted against the behaviour catalogue, not the marker
    string.
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
    """The committed case's turns cost what a real request costs, not `Behaviour`'s default.

    The bill is `input_tokens_per_char` over the serialized request, which includes instructions,
    skills listing and tool schemas, so a measured turn bills orders of magnitude more than 900.
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
    """The recorded turns pass and more expensive ones fail with a nonzero exit.

    Only the front door and the ledger are substituted; the metric, recorded case, drift band and
    verdict are the shipped ones.
    """
    recorded = _recorded_case_turns()

    def _serve(turns: list[TurnCost]) -> None:
        async def _drive(base_url: str) -> lane.Driven:
            return lane.Driven("fixed-session", ())

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

    async def _drive(base_url: str) -> lane.Driven:
        return lane.Driven("fixed-session", ())

    async def _none(session_id: str) -> list[TurnCost]:
        return []

    monkeypatch.setattr(lane, "_drive", _drive)
    monkeypatch.setattr(lane, "_recorded", _none)
    assert lane.main([]) == 3


def _front_door(scrapes: list[str]) -> ThreadingHTTPServer:
    """A socket speaking the three routes `_drive` uses; `/metrics` answers `scrapes` in order."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._reply(scrapes.pop(0))

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self._reply('{"session_id": "s1"}' if self.path == "/sessions" else "data: {}\n\n")

        def _reply(self, body: str) -> None:
            payload = body.encode()
            self.send_response(200)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_run_that_lost_a_connector_is_not_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that ran without a connector's tools was sent another prompt, so it is not scored.

    Two boots of one commit billed 900,198 and 429,076 for the same questions because a handshake
    outlasting the open bound drops that bundle's tools for the breaker window while `/readyz`
    stays green. Driven through `main` against a real socket: the recorded turns exit 0 while the
    front door's counter is still, exit 3 when it moves between the first and last question, and a
    connector already absent before the run is not the run's.
    """
    recorded = _recorded_case_turns()

    async def _read(session_id: str) -> list[TurnCost]:
        return recorded

    monkeypatch.setattr(lane, "_recorded", _read)
    still = "chemclaw_durable_unreachable_total 0.0\n"

    def seven(count: int) -> str:
        return f'chemclaw_connectors_unreachable_total{{connector="bo"}} {count}.0\n'

    for scrapes, expected in (
        ([still, still], 0),
        ([still, seven(3)], 3),
        ([seven(7), seven(9)], 3),
        ([seven(7), seven(7)], 0),
    ):
        server = _front_door(scrapes)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            assert lane.main(["--base-url", url]) == expected, scrapes
        finally:
            server.shutdown()
            server.server_close()


def test_the_emitted_case_carries_no_identity(tmp_path: Path) -> None:
    """An emitted case records what a turn cost, never who asked for it.

    The emitted keys are exactly `_EMITTED`, a strict subset of `TurnCost`: that excludes `actor`,
    `session_id` and defaults that would pose as measurements, and makes `include=` a real
    narrowing.
    """
    lane._emit(_recorded_case_turns(), tmp_path / "case.md", None)
    text = (tmp_path / "case.md").read_text(encoding="utf-8")
    assert "input_tokens" in text
    for field in ("actor", "session_id", "turn_id", "outcome"):
        assert f'"{field}"' not in text

    body = json.loads(text.split("---", 2)[1])
    emitted_keys = {key for turn in body["output"]["turns"] for key in turn}
    assert emitted_keys == set(lane._EMITTED)
    assert emitted_keys < set(TurnCost.model_fields), (
        "the emitted case carries every field TurnCost has, so `include=` narrows nothing and a "
        "future field reaches the case file unasked"
    )


_SURFACE = {"tools": 128, "schema_bytes": 294_508}


def test_a_run_sent_another_tool_surface_is_not_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same questions over a different surface are a different workload, so exit 3, unscored.

    One workload read 0.90, 1.00 and 1.29 on three lane configurations, each deterministic: the
    score followed the connectors and profiles the lane booted with. The recorded turns over the
    recorded surface pass; the recorded turns over another surface are refused.
    """
    recorded = _recorded_case_turns()
    case = EvalCase(
        id=lane.CASE_ID,
        metrics=["turn_cost_ratio"],
        output={"turns": [t.model_dump(include=set(lane._EMITTED)) for t in recorded]},
        reference={"baseline_tokens": lane.BASELINE_TOKENS, "surface": _SURFACE},
    )

    async def _drive(base_url: str) -> lane.Driven:
        return lane.Driven("fixed-session", ())

    async def _read(session_id: str) -> list[TurnCost]:
        return recorded

    monkeypatch.setattr(lane, "_drive", _drive)
    monkeypatch.setattr(lane, "_recorded", _read)
    monkeypatch.setattr(lane, "_recorded_case", lambda: case)
    for surface, expected in ((_SURFACE, 0), ({"tools": 97, "schema_bytes": 209_089}, 3)):

        async def _seen(surface: dict[str, int] = surface) -> dict[str, int]:
            return surface

        monkeypatch.setattr(lane, "_mock_surface", _seen)
        assert lane.main([]) == expected, surface


def test_the_emitted_case_records_the_surface_it_was_sent(tmp_path: Path) -> None:
    """`--emit` writes the surface beside the turns, so the next run has something to compare."""
    lane._emit(_recorded_case_turns(), tmp_path / "case.md", _SURFACE)
    body = json.loads((tmp_path / "case.md").read_text(encoding="utf-8").split("---", 2)[1])
    assert body["reference"]["surface"] == _SURFACE


def test_the_mock_keeps_the_surface_of_the_last_request_that_sent_tools() -> None:
    """A tool-less request (a compaction summary) leaves the reading at the agent's own surface."""
    mock = MockLlm([Behaviour(name="plain")])
    tools = [{"type": "function", "function": {"name": "a", "parameters": {}}}]
    mock.note_surface({"tools": tools})
    mock.note_surface({"messages": []})
    assert mock.last_surface == {
        "tools": 1,
        "schema_bytes": len(json.dumps(tools, sort_keys=True).encode("utf-8")),
    }
