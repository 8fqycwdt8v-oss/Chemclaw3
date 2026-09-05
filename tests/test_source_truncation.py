"""A source that cuts before the merge must say so, or a cut looks like a corpus.

`EvidenceSweep` was built so "a cut does not look like a corpus" — and it was true of the *merge*
and false of the *legs*. `truncated_by` names which of `gather_evidence_max_chunks`/`_max_chars` cut
the merged list, and `total_before_cap` counts what survived merging. Neither can see a retriever
that truncated before handing anything over.

That is the only cut that ever bites on the shipped configuration. `retrieval_top_k` is 8 and the
merge caps are 40 chunks / 60,000 chars, so with at most three text legs the merge bound is
unreachable while the per-leg bound fires on any broad question. Measured on 5,000 notes that all
matched every term, `gather_evidence` reported `chunks=8, total_before_cap=8, truncated_by=None` —
the model told "8 found, nothing was cut" about a corpus it had seen 0.16% of.

**An absence in `sources_truncated` is "cut nothing, or cannot tell", never "cut nothing".** The
graph leg scores every eligible note and then truncates, so it knows both numbers. The dense and
lexical legs push `LIMIT k` into the index and do not know what they did not fetch; reporting a zero
for them would assert "nothing was cut" on the one leg that cannot check.
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
    """`found is None` is the honest answer for a leg that pushed `LIMIT k` into its index.

    A zero here would read as "this leg cut nothing", which is the ambiguous zero
    `D-2026-08-03-a-metric-must-declare-what-it-can-see` is about — a claim of completeness from
    the one leg that cannot check it. `dropped` is 0 for both cases, which is why the *absence*
    from `sources_truncated` carries the distinction rather than the number.
    """
    unknown = Hits([EvidenceChunk(content="x", source_note_id="n-1", retriever="vector")])
    assert unknown.found is None
    assert unknown.dropped == 0


def test_hits_is_a_list_so_every_existing_caller_keeps_working() -> None:
    """The reason this is a `list` subclass rather than a wrapper.

    84 call sites consume `retrieve()` and 81 of them are tests doing `len(...)`, `== []` and
    iteration. A wrapper would have churned all of them for no behavioural gain in any; a `Hits`
    *is* a list, so only the two ends that care about the count changed.
    """
    chunks = [EvidenceChunk(content="x", source_note_id="n-1", retriever="graph")]
    hits = Hits(chunks, found=99)

    assert hits == chunks
    assert len(hits) == 1
    assert [chunk.source_note_id for chunk in hits] == ["n-1"]
    assert Hits() == []


def test_the_sweep_carries_the_per_leg_cut_to_the_model(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """End to end: the fact survives the fan-out, the merge and the framing.

    Driven through `gather_evidence` rather than the retriever, because every intermediate step is
    somewhere the count could be dropped — and one of them did drop it: the fan-out copied its
    branch's chunks into a plain `list`, three lines from where the count was produced.
    """
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
