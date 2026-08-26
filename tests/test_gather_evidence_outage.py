"""`gather_evidence` must not report an outage as "nothing on file".

The tool's docstring is the model's contract, and it says an empty result means *nothing on file,
never invented*. Every evidence source swallowed its own failure into an empty list, so a chemist
asking "have we run this nitration before?" during a Postgres blip was told, confidently, that the
company has no prior art — with nothing on the stream or in the answer saying a source was down.

The fix is narrow on purpose. A single flaky source still costs its own leg and not the turn, which
is the trade `fanout._sweep` argues for and is right about. What changed is the case where *nothing*
could be asked: there is no honest empty answer to give, so the tool raises and the model reports a
failure it can say out loud.
"""

import asyncio

import pytest

from chemclaw.agent import research_tools
from chemclaw.core.errors import ChemclawError
from chemclaw.retrieval.evidence import EvidenceChunk, EvidenceSweep


class _Dead:
    """A source whose backing store is unreachable."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        raise ConnectionError(f"{self.name}: connection refused")


class _Live:
    """A source that answers — with `count` hits, possibly none."""

    def __init__(self, name: str, count: int = 0) -> None:
        self.name = name
        self._count = count

    async def retrieve(self, _query: str, _filters: dict[str, object]) -> list[EvidenceChunk]:
        return [
            EvidenceChunk(
                content=f"hit {index}",
                source_note_id=f"note-{index}",
                retriever=self.name,
                score=0.5,
            )
            for index in range(self._count)
        ]


def _gather() -> EvidenceSweep:
    """Call the tool's underlying coroutine, as the agent would."""
    return asyncio.run(research_tools.gather_evidence(query="have we run this nitration"))


def test_every_source_down_raises_instead_of_reporting_nothing_on_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: an outage presented to a chemist as an absence of prior art."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Dead("graph")), ("lexical", _Dead("lexical"))],
    )

    with pytest.raises(ChemclawError) as excinfo:
        _gather()

    message = str(excinfo.value)
    assert "graph" in message and "lexical" in message, (
        "the error must name which sources were unavailable, or an operator cannot act on it"
    )


def test_all_sources_healthy_and_empty_is_still_a_real_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. A quiet search is a legitimate result and must not become an error."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Live("graph")), ("lexical", _Live("lexical"))],
    )

    sweep = _gather()
    assert sweep.chunks == []
    # And it says so as an absence rather than as a degradation: nothing failed, nothing was cut.
    assert sweep.sources_failed == [] and sweep.truncated_by is None


def test_one_source_down_still_answers_from_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single flaky source costs its own leg, not the turn — deliberately unchanged."""
    monkeypatch.setattr(
        research_tools,
        "_sources",
        lambda _anchor: [("graph", _Live("graph", 2)), ("lexical", _Dead("lexical"))],
    )

    sweep = _gather()
    assert len(sweep.chunks) == 2
    # **The half that used to be invisible.** A partial outage returned real-but-incomplete
    # evidence with the degradation visible only on the stream, so a chemist reading the tool's
    # result could not tell this from a corpus that genuinely holds two chunks.
    assert sweep.sources_failed == ["lexical"]
