"""A source that cuts before the merge must say so, or a cut looks like a corpus.

`truncated_by` and `total_before_cap` describe only the merge; on the shipped configuration the
per-leg `retrieval_top_k` is the cut that bites. An absence in `sources_truncated` means "cut
nothing, or cannot tell": the graph leg knows both counts, while legs that push `LIMIT k` into
the index cannot.
"""

import asyncio
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.retrieval.evidence import EvidenceChunk, Hits
from chemclaw.retrieval.retrievers import GraphRetriever

_NOTE = "---\nid: {id}\ntype: reaction\nconfidence: 0.85\ncreated_by: human\n---\n\n{body}\n"


def _corpus(directory: Path, count: int) -> None:
    """`count` notes that all match every term of the query below."""
    for index in range(count):
        note_id = f"reaction-{index:05d}"
        (directory / f"{note_id}.md").write_text(
            _NOTE.format(id=note_id, body=f"Suzuki coupling run {index}. The yield was recorded."),
            encoding="utf-8",
        )


def test_the_graph_leg_reports_what_its_own_bound_discarded(tmp_path: Path) -> None:
    """The number that was invisible: 200 matched, 8 returned, 192 dropped inside the retriever."""
    _corpus(tmp_path, 200)
    hits = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("Suzuki coupling yield", {}))

    assert isinstance(hits, Hits)
    assert len(hits) == settings.retrieval_top_k
    assert hits.found == 200
    assert hits.dropped == 200 - settings.retrieval_top_k


def test_a_leg_that_did_not_cut_reports_dropping_nothing(tmp_path: Path) -> None:
    """Below the bound there is nothing to report, and `dropped` must not invent a cut."""
    _corpus(tmp_path, 3)
    hits = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("Suzuki coupling yield", {}))

    assert len(hits) == 3
    assert hits.found == 3
    assert hits.dropped == 0


def test_a_source_that_cannot_say_reports_unknown_rather_than_zero(tmp_path: Path) -> None:
    """A leg that pushed `LIMIT k` into its index reports `found is None`, not zero.

    A zero would claim completeness; the absence from `sources_truncated` carries the distinction.
    """
    unknown = Hits([EvidenceChunk(content="x", source_note_id="n-1", retriever="vector")])
    assert unknown.found is None
    assert unknown.dropped == 0


def test_hits_is_a_list_so_every_existing_caller_keeps_working() -> None:
    """`Hits` is a `list` subclass, so existing `retrieve()` callers keep working unchanged."""
    chunks = [EvidenceChunk(content="x", source_note_id="n-1", retriever="graph")]
    hits = Hits(chunks, found=99)

    assert hits == chunks
    assert len(hits) == 1
    assert [chunk.source_note_id for chunk in hits] == ["n-1"]
    assert Hits() == []


def test_the_sweep_carries_the_per_leg_cut_to_the_model(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The per-leg cut survives the fan-out, the merge and the framing, via `gather_evidence`."""
    from chemclaw.agent.research_tools import gather_evidence

    notes = tmp_path / "knowledge" / "reaction"
    notes.mkdir(parents=True)
    _corpus(notes, 200)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge", raising=False)

    sweep = asyncio.run(gather_evidence("Suzuki coupling yield"))

    assert len(sweep.chunks) == settings.retrieval_top_k
    assert sweep.sources_truncated == {"graph": 200 - settings.retrieval_top_k}
    # And the merge-level fields still say what they always said — two different facts, two fields.
    assert sweep.truncated_by is None
    assert sweep.sources == {"graph": settings.retrieval_top_k}
