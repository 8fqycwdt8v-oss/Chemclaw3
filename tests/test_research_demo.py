"""Test that the end-to-end research-loop demo runs and produces a cited, computed answer.

Guards the credential-free walkthrough (`examples/research_demo.py`) as real behavior: it must
gather cited evidence, cross-learn structurally, compute the untried solvent's property
proactively, and propose a next experiment — the whole loop, no LLM, no database. This is the
harness that shows the agent's tools composing an answer without live credentials.
"""

from examples.research_demo import run_demo


def test_demo_produces_a_cited_computed_answer() -> None:
    """The transcript cites source notes, includes a real prediction, and a next experiment."""
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
