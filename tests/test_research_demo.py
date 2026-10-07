"""Test that the end-to-end research-loop demo runs and produces a cited, computed answer.

`examples/research_demo.py` must gather cited evidence, cross-learn structurally, compute the
untried solvent's property and propose a next experiment, with no LLM and no database. The calc
server is supplied by `tests/calc_server_fake.py`, so the real client, cache and tool are driven.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from examples.research_demo import run_demo
from tests.calc_server_fake import FakeCalcServer, install


def test_demo_produces_a_cited_computed_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transcript cites source notes, includes a real prediction, and a next experiment."""
    install(monkeypatch, FakeCalcServer())
    transcript = run_demo()

    # Evidence is cited by note id (section 1 + the composed answer).
    assert "[[optimization-ester]]" in transcript
    assert "[[reaction-ester-80c]]" in transcript
    # The untried solvent was evaluated proactively with the real ESOL model.
    assert "2-MeTHF (UNTRIED)" in transcript
    assert "esol-delaney@2004" in transcript
    # A next experiment was proposed inside the declared space.
    assert "Suggested next experiment" in transcript
    assert "solvent" in transcript


def test_the_documented_command_refuses_rather_than_crashing_without_the_calc_server() -> None:
    """Without the calc server, the documented command refuses rather than crashing.

    Driven as a subprocess against a port nothing listens on, since the `__main__` block is what
    is under test.
    """
    result = subprocess.run(
        [sys.executable, "-m", "examples.research_demo"],
        capture_output=True,
        text=True,
        timeout=300,
        # A bare environment on purpose: no inherited `CHEMCLAW_*` override can point this at a
        # server that happens to be up in the session that runs the suite.
        env={"CHEMCLAW_CALC_SERVER_URL": "http://127.0.0.1:1/mcp", "PATH": "/usr/bin:/bin"},
        cwd=Path(__file__).resolve().parents[1],
    )

    assert result.returncode != 0
    assert "CHEMCLAW_CALC_SERVER_URL" in result.stderr
    # The refusal is the last thing said, not a stack frame — the property a reader experiences.
    assert result.stderr.strip().splitlines()[-1].startswith("This walkthrough needs")
