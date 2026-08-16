"""Test that the end-to-end research-loop demo runs and produces a cited, computed answer.

Guards the credential-free walkthrough (`examples/research_demo.py`) as real behavior: it must
gather cited evidence, cross-learn structurally, compute the untried solvent's property
proactively, and propose a next experiment — the whole loop, no LLM, no database. This is the
harness that shows the agent's tools composing an answer without live credentials.

The solubility model left for `Chemclaw3-mcp` (`D-2026-08-16-the-physics-leaves-the-cache-stays`),
so the one step that used to compute in-process now crosses a wire. The demo names that dependency;
this file supplies it as `tests/calc_server_fake.py`, which keeps the whole loop covered with
nothing running and still drives the real client, the real cache and the real tool.
"""

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
