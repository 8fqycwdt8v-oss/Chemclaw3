"""Answer verification (plan F10-B): deterministic citation gate + LLM-as-judge, both offline.

The deterministic path (verifier off, the default) reuses the report citation check, so a fabricated
citation is caught with no network. The LLM path (verifier on) is exercised with a fake structured
client, proving it returns the judge's verdict and degrades to the deterministic gate when the model
yields nothing parseable. `gather_cited_evidence` resolves an answer's `[[wikilinks]]` to the notes
it cites, which is what the runner scores a conversational turn against.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agents.verifier import (
    ClaimCheck,
    VerificationResult,
    gather_cited_evidence,
    verify_answer,
    verify_turn_answer,
)
from chemclaw.config import settings
from report.evidence import EvidenceChunk


class _FakeResponse:
    """A stand-in for a MAF `ChatResponse`, carrying only the parsed structured `value`."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeVerifierClient:
    """A fake chat client whose `get_response` returns a preset structured value."""

    def __init__(self, value: Any) -> None:
        self._value = value
        self.response_formats: list[Any] = []

    async def get_response(self, prompt: str, *, response_format: Any) -> _FakeResponse:
        self.response_formats.append(response_format)
        return _FakeResponse(self._value)


def _chunk(note_id: str, content: str = "some evidence") -> EvidenceChunk:
    return EvidenceChunk(content=content, source_note_id=note_id, retriever="graph")


def test_deterministic_flags_fabricated_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier off: an answer citing a note that was not retrieved is unsupported, confidence 0."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_answer("Yield was 90% [[reaction-x]].", [_chunk("reaction-y")]))
    assert result.confidence == 0.0
    assert result.unsupported and result.unsupported[0].cited_note_id == "reaction-x"


def test_deterministic_passes_grounded_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier off: an answer whose every citation was retrieved is supported, confidence 1."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_answer("Yield was 90% [[reaction-a]].", [_chunk("reaction-a")]))
    assert result.confidence == 1.0
    assert not result.unsupported


def test_deterministic_uncited_answer_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier off: an answer with no citations is not flagged (the gate catches fabrication)."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_answer("A general remark with no citation.", []))
    assert result.confidence == 1.0
    assert not result.unsupported


def test_llm_verifier_returns_the_judges_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: the structured judge verdict (a low-confidence unsupported claim) comes back."""
    monkeypatch.setattr(settings, "verifier_enabled", True)
    verdict = VerificationResult(
        claims=[ClaimCheck(text="fabricated stat", supported=False, cited_note_id="reaction-z")],
        confidence=0.0,
    )
    client = _FakeVerifierClient(verdict)
    result = asyncio.run(
        verify_answer("An answer [[reaction-z]].", [_chunk("reaction-z")], client=client)
    )
    assert result is verdict
    assert client.response_formats == [VerificationResult]  # structured output requested


def test_llm_verifier_falls_back_when_no_structured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a model that yields no parseable value degrades to the deterministic gate."""
    monkeypatch.setattr(settings, "verifier_enabled", True)
    client = _FakeVerifierClient(None)
    result = asyncio.run(
        verify_answer("Yield was 90% [[reaction-x]].", [_chunk("reaction-y")], client=client)
    )
    assert result.confidence == 0.0  # deterministic gate caught the fabricated citation


def test_llm_verifier_falls_back_when_the_client_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifier on: a failing judge endpoint degrades to the deterministic gate, never unscored."""
    monkeypatch.setattr(settings, "verifier_enabled", True)

    class _ExplodingClient:
        async def get_response(self, prompt: str, *, response_format: Any) -> Any:
            raise RuntimeError("verifier endpoint down")

    result = asyncio.run(
        verify_answer(
            "Yield was 90% [[reaction-x]].", [_chunk("reaction-y")], client=_ExplodingClient()
        )
    )
    assert result.confidence == 0.0  # the offline citation gate still caught the fabrication


def _write_note(directory: Path, note_id: str, body: str) -> None:
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: reaction\n---\n{body}\n", encoding="utf-8"
    )


def test_gather_cited_evidence_loads_only_cited_and_existing_notes(tmp_path: Path) -> None:
    """Only the notes the answer cites *and* that exist on disk become evidence."""
    _write_note(tmp_path, "reaction-a", "amide coupling gave 90%")
    _write_note(tmp_path, "reaction-b", "unrelated distillation")
    evidence = asyncio.run(
        gather_cited_evidence(
            "See [[reaction-a]] and [[reaction-missing]].", notes_dir=str(tmp_path)
        )
    )
    assert [chunk.source_note_id for chunk in evidence] == ["reaction-a"]  # b uncited, missing gone


def test_verify_turn_answer_flags_dangling_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversational answer citing a non-existent note is scored unsupported (deterministic)."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    _write_note(tmp_path, "reaction-a", "real note")
    result = asyncio.run(verify_turn_answer("Cites [[reaction-ghost]].", notes_dir=str(tmp_path)))
    assert result.confidence == 0.0 and result.unsupported
