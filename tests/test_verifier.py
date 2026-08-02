"""Answer verification (plan F10-B): deterministic citation gate + LLM-as-judge, both offline.

The deterministic path (verifier off, the default) reuses the report citation check, so a fabricated
citation is caught with no network. The LLM path (verifier on) is exercised with a fake structured
client, proving it returns the judge's verdict and degrades to the deterministic gate when the model
yields nothing parseable. `turn_evidence` builds the evidence a conversational answer is scored
against out of what the turn's tools actually returned, and `ungrounded_parameter_shapes` is the
deterministic scan for method parameters no tool in the turn produced.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.agent.verifier import (
    ClaimCheck,
    VerificationResult,
    turn_evidence,
    ungrounded_parameter_shapes,
    verify_answer,
    verify_turn_answer,
)
from chemclaw.core.config import settings
from chemclaw.retrieval.evidence import EvidenceChunk


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


def test_deterministic_uncited_answer_is_unverified_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer that cites nothing is unverified — the metric must not reward citing nothing.

    This returned `confidence=1.0` and no unsupported claim, which made the score maximal exactly
    where the answer was least anchored. In the 190-probe live run 0 of 33 analytical answers
    carried a single wikilink, so every fabricated method in that slice earned a perfect
    citation-faithfulness result and `review_required=False`.
    """
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(
        verify_answer("Use a 1.0 mL/min flow rate at 254 nm.", [_chunk("reaction-a")])
    )
    assert result.confidence == 0.0
    assert result.unsupported and result.unsupported[0].cited_note_id is None
    assert result.confidence < settings.verifier_confidence_threshold


def test_an_empty_answer_is_not_routed_to_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one exception: a turn that produced no text has nothing to be unverified about."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_answer("   ", [_chunk("reaction-a")]))
    assert result.confidence == 1.0
    assert not result.unsupported


def test_an_unreachable_judge_does_not_certify_an_uncited_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verifier that could not run must not produce a *stronger* signal than one that did.

    The degradation path returned the deterministic gate's verdict, and for an ordinary chat answer
    — which carries no `[[wikilinks]]` — that gate has nothing to check and said `confidence=1.0`.
    So with the judge endpoint down, every answer in the deployment came back maximally confident
    with `review_required=False`, while nothing had been verified at all. The failure was invisible
    on precisely the surface that exists to make it visible.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)

    class _Broken:
        async def get_response(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("verifier endpoint unreachable")

    result = asyncio.run(verify_answer("A general remark with no citation.", [], client=_Broken()))
    assert result.confidence == 0.0
    assert result.unsupported, "an unverifiable answer must be routed to a human, not certified"
    assert result.confidence < settings.verifier_confidence_threshold


def test_a_working_judge_still_certifies_an_uncited_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard above must not fire on the healthy path — only degradation changes the verdict."""
    monkeypatch.setattr(settings, "verifier_enabled", True)
    verdict = VerificationResult(
        claims=[ClaimCheck(text="a general remark", supported=True)], confidence=1.0
    )
    result = asyncio.run(
        verify_answer("A general remark with no citation.", [], client=_FakeVerifierClient(verdict))
    )
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


def test_turn_evidence_grounds_a_citation_in_the_tool_result_that_returned_it() -> None:
    """A cited id is evidence only when a tool result in *this turn* mentions it.

    Substring containment against the result text, so a tool that renders ids as wikilinks, as
    bare slugs or inside JSON is read identically. `reaction-b` is returned by a tool and never
    cited, which must not make it evidence for a claim; `reaction-x` is cited and never returned.
    """
    outputs = ['{"notes": ["reaction-a", "reaction-b"]}']
    evidence = turn_evidence("From [[reaction-a]] and [[reaction-x]].", outputs)
    assert [chunk.source_note_id for chunk in evidence] == ["reaction-a"]
    assert evidence[0].content == outputs[0]


def test_turn_evidence_keeps_an_uncited_tool_result_under_an_unciteable_id() -> None:
    """A result no citation matched is still evidence to read, never grounding to claim.

    The judge has to see everything the turn retrieved to check the answer's prose, but a
    synthetic `tool-output-N` id is one no `[[wikilink]]` can accidentally resolve to — so adding
    it cannot turn an ungrounded citation into a grounded one.
    """
    evidence = turn_evidence("No citations here.", ["pKa 15.9", "", "  "])
    assert [chunk.source_note_id for chunk in evidence] == ["tool-output-0"]


def test_a_citation_the_turn_never_saw_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversational gate scores against the turn, not against the graph on disk."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_turn_answer("Cites [[reaction-a]].", ["pKa 15.9, no note ids"]))
    assert result.confidence == 0.0
    assert result.unsupported[0].cited_note_id == "reaction-a"


def test_a_citation_the_turn_did_see_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same answer, with a tool result that actually returned the note, is supported."""
    monkeypatch.setattr(settings, "verifier_enabled", False)
    result = asyncio.run(verify_turn_answer("Cites [[reaction-a]].", ["found [[reaction-a]]"]))
    assert result.confidence == 1.0
    assert not result.unsupported


def test_a_method_parameter_no_tool_produced_is_named() -> None:
    """The live run's failure in one line: a branded method table with no analytical capability.

    Each shape is reported with the text that matched, because "something fired" is not something
    a reviewer can act on.
    """
    answer = (
        "Use a Kinetex C18 column, 1.0 mL/min, 5-95% B over 12 min, detection at 254 nm, "
        "back-pressure 4500 psi. Impurity limits: 60 ug/day, 5 ppm. Assay against Form II."
    )
    found = ungrounded_parameter_shapes(answer, ["gather_evidence returned nothing relevant"])
    assert found == [
        "flow rate: 1.0 mL/min",
        "gradient %B: 5-95% B",
        "wavelength: 254 nm",
        "pressure: 4500 psi",
        "column brand: Kinetex",
        "ICH daily limit: 60 ug/day",
        "ppm limit: 5 ppm",
        "polymorph form: Form II",
    ]


def test_a_parameter_class_some_tool_produced_is_left_alone() -> None:
    """Per shape *class*, not per value: an answer reasoning about a retrieved number is clean.

    Comparing values instead would flag every answer that rounds or reformats one, and a
    heuristic that fires on a legitimate answer is worse than no heuristic. The wavelength here
    is ungrounded and still caught, so the class check is doing work rather than passing
    everything.
    """
    answer = "Run it at 0.8 mL/min and detect at 254 nm."
    found = ungrounded_parameter_shapes(answer, ["method: 1.0 mL/min on a C18 column"])
    assert found == ["wavelength: 254 nm"]


def test_ordinary_chemistry_prose_does_not_trip_the_scan() -> None:
    r"""The over-firing the pattern table is shaped to avoid: "to form a complex" is not a form.

    Matched case-insensitively, `\bform\s+[A-D]\b` hits every sentence of ordinary prose that
    says "form a", which would make the gate fire on almost every legitimate answer and cost a
    chemist their trust in every mark that follows.
    """
    prose = "The base deprotonates the amide to form a stabilised anion; warming drives it to bar."
    assert ungrounded_parameter_shapes(prose, []) == []


def test_a_verifier_that_cannot_be_built_still_gets_the_offline_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping the switch without a reachable model must not leave the answer unscored.

    Constructing the client used to sit above the guard, so a deployment turning verification on
    for the first time — the moment its `"verifier"` route is most likely to be missing — got an
    exception out of `verify_answer` and, through the runner's own guard, a plain unscored answer.
    Every failure mode of the judge now lands on the offline citation gate, which is what the
    contract says and what makes the switch worth flipping.
    """
    monkeypatch.setattr(settings, "verifier_enabled", True)

    def _cannot_build() -> Any:
        raise RuntimeError("no model route configured for 'verifier'")

    monkeypatch.setattr("chemclaw.agent.verifier._default_client", _cannot_build)
    result = asyncio.run(verify_answer("Yield was 90% [[reaction-x]].", [_chunk("reaction-y")]))
    assert result.confidence == 0.0  # the offline gate caught the fabricated citation
    assert result.unsupported[0].cited_note_id == "reaction-x"
