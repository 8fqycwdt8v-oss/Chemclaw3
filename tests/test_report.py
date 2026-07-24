"""Behavioral tests for the report harness (plan Phase 5b), runnable without a server.

Proves the CHECKMATE 5b acceptance: a request produces a sectioned draft where every
statement links a source note, unsupported sections are marked rather than hallucinated,
fabricated claims are discarded by the verify step, and the harness core is source-agnostic
(works with a fake retriever and with the real graph / fingerprint retrievers).
"""

import asyncio
from pathlib import Path
from typing import Any

from mcp_servers.fpstore import InMemoryFingerprintStore
from mcp_servers.rxnfp.search import record_for_reaction
from report.evidence import EvidenceChunk, SourceRetriever
from report.harness import (
    Claim,
    Report,
    ReportRequest,
    ReportSection,
    SynthesizedSection,
    gather_section,
    report_note,
    verify_claims,
)
from report.retrievers import FingerprintReactionRetriever, GraphRetriever

_ESTER = "CCO.CC(=O)O>>CCOC(C)=O"


async def _gather(request: ReportRequest, retrievers: list[SourceRetriever]) -> Report:
    """Assemble a whole Report from per-section gathers (the workflow does this durably)."""
    sections = [await gather_section(section, retrievers) for section in request.sections]
    return Report(title=request.title, sections=sections)


class _FakeRetriever:
    """A retriever returning canned evidence for a keyword — the source-agnostic seam."""

    name = "fake"

    def __init__(self, keyword: str, chunks: list[EvidenceChunk]) -> None:
        self._keyword = keyword
        self._chunks = chunks

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return self._chunks if self._keyword in query else []


def _request(*sections: ReportSection) -> ReportRequest:
    return ReportRequest(title="Development report", sections=list(sections))


# --- harness core (5b.1) --------------------------------------------------------------


def test_gather_marks_unsupported_section_instead_of_inventing() -> None:
    """A section with no retrieved evidence is kept but marked unsupported (no hallucination)."""

    async def _run() -> None:
        chunk = EvidenceChunk(
            content="Yield rose to 85%.", source_note_id="reaction-a", retriever="fake"
        )
        retriever = _FakeRetriever("yield", [chunk])
        report = await _gather(
            _request(
                ReportSection(heading="Yield", query="yield trend", memory_layer="episodic"),
                ReportSection(heading="Toxicity", query="tox data", memory_layer="evidence"),
            ),
            [retriever],
        )
        assert report.sections[0].supported is True
        assert report.sections[1].supported is False  # no evidence for toxicity
        text = report_note(report).body
        assert "No supporting data found" in text  # marked, not fabricated
        assert "[[reaction-a]]" in text  # supported claim cites its source
        assert "[layer: episodic]" in text and "[layer: evidence]" in text  # layers declared

    asyncio.run(_run())


def test_failed_section_renders_distinctly_from_empty() -> None:
    """A `retrieval_failed` section is unsupported and rendered as failed, not as 'no data'."""
    failed = SynthesizedSection(
        heading="Yield", memory_layer="episodic", evidence=[], retrieval_failed=True
    )
    empty = SynthesizedSection(heading="Toxicity", memory_layer="evidence", evidence=[])
    assert failed.supported is False and empty.supported is False
    text = report_note(Report(title="R", sections=[failed, empty])).body
    assert "Retrieval failed" in text  # the errored section is flagged as incomplete
    assert "No supporting data found" in text  # the genuinely empty section reads differently


def test_report_note_cites_every_source() -> None:
    """Every evidence chunk in the draft wikilinks its source note (5b.7)."""

    async def _run() -> None:
        chunks = [
            EvidenceChunk(content="A", source_note_id="reaction-a", retriever="fake"),
            EvidenceChunk(content="B", source_note_id="campaign-b", retriever="fake"),
        ]
        report = await _gather(
            _request(ReportSection(heading="S", query="k", memory_layer="episodic")),
            [_FakeRetriever("k", chunks)],
        )
        note = report_note(report)
        assert note.type == "report"
        assert set(note.outgoing_links()) == {"reaction-a", "campaign-b"}

    asyncio.run(_run())


def test_report_id_is_ref_safe_and_unique() -> None:
    """The report id is a valid git-ref/path (no punctuation) and unique per exact title."""

    async def _run() -> None:
        async def _note(title: str) -> str:
            report = await _gather(
                ReportRequest(
                    title=title,
                    sections=[ReportSection(heading="S", query="q", memory_layer="episodic")],
                ),
                [],
            )
            return report_note(report).id

        punct = await _note("Q3: Yield/Cost Analysis!")
        assert set(punct) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")  # ref/path-safe
        # Titles that slug alike stay distinct via the title hash (no collision/overwrite).
        assert await _note("Widget Development") != await _note("widget development")

    asyncio.run(_run())


# --- adversarial verify (5b.4) --------------------------------------------------------


def test_verify_discards_unsupported_and_fabricated_claims() -> None:
    """Only claims whose citations were actually retrieved survive; the rest are dropped."""
    evidence = [EvidenceChunk(content="x", source_note_id="reaction-a", retriever="fake")]
    claims = [
        Claim(text="Backed by real evidence.", citations=["reaction-a"]),
        Claim(text="Fabricated 40% trend.", citations=["reaction-ghost"]),  # unknown source
        Claim(text="Uncited assertion.", citations=[]),  # no citation at all
    ]
    supported, discarded = verify_claims(claims, evidence)
    assert [c.text for c in supported] == ["Backed by real evidence."]
    assert {c.text for c in discarded} == {"Fabricated 40% trend.", "Uncited assertion."}


# --- concrete retrievers (5b.3) -------------------------------------------------------


def test_graph_retriever_matches_and_cites_notes(tmp_path: Path) -> None:
    """The graph retriever returns citable chunks from notes matching the query + filters."""

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: reaction-a\ntype: reaction\ntags: [proj-x]\n---\nEsterification at 80 C.\n",
            encoding="utf-8",
        )
        (tmp_path / "b.md").write_text(
            "---\nid: playbook-b\ntype: playbook\n---\nUnrelated distillation.\n", encoding="utf-8"
        )
        retriever = GraphRetriever(str(tmp_path))

        hits = await retriever.retrieve("esterification", {"type": "reaction"})
        assert [c.source_note_id for c in hits] == ["reaction-a"]
        assert hits[0].retriever == "graph"
        # A type filter excludes the playbook even if the query would match it.
        assert await retriever.retrieve("distillation", {"type": "reaction"}) == []

    asyncio.run(_run())


def test_graph_retriever_scores_by_confidence(tmp_path: Path) -> None:
    """Each chunk carries a score from its note's confidence, defaulting when absent (KM-5)."""
    from chemclaw.config import settings

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: reaction-a\ntype: reaction\nconfidence: 0.7\n---\nEsterification.\n",
            encoding="utf-8",
        )
        (tmp_path / "b.md").write_text(
            "---\nid: reaction-b\ntype: reaction\n---\nEsterification.\n", encoding="utf-8"
        )
        hits = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})
        by_id = {c.source_note_id: c.score for c in hits}
        assert by_id["reaction-a"] == 0.7
        assert by_id["reaction-b"] == settings.retrieval_default_confidence

    asyncio.run(_run())


def test_graph_retriever_ranks_hits_by_score_not_disk_order(tmp_path: Path) -> None:
    """Graph hits come back best-first (KM-5), not in alphabetical file order (the RRF contract)."""

    async def _run() -> None:
        (tmp_path / "aaa.md").write_text(
            "---\nid: reaction-aaa\ntype: reaction\nconfidence: 0.2\n---\nEsterification.\n",
            encoding="utf-8",
        )
        (tmp_path / "zzz.md").write_text(
            "---\nid: reaction-zzz\ntype: reaction\nconfidence: 0.9\n---\nEsterification.\n",
            encoding="utf-8",
        )
        hits = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})
        assert [c.source_note_id for c in hits] == ["reaction-zzz", "reaction-aaa"]

    asyncio.run(_run())


def test_graph_retriever_excludes_expired_notes(tmp_path: Path) -> None:
    """A report never cites a note past its `valid_to` as current evidence (KM-7)."""

    async def _run() -> None:
        (tmp_path / "old.md").write_text(
            "---\nid: reaction-old\ntype: reaction\nvalid_to: 2000-01-01\n---\nEsterification.\n",
            encoding="utf-8",
        )
        (tmp_path / "new.md").write_text(
            "---\nid: reaction-new\ntype: reaction\n---\nEsterification, current.\n",
            encoding="utf-8",
        )
        hits = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})
        assert [c.source_note_id for c in hits] == ["reaction-new"]

    asyncio.run(_run())


def test_graph_retriever_excerpt_strips_wikilinks(tmp_path: Path) -> None:
    """An excerpt never carries a source note's `[[wikilink]]` into the report verbatim.

    A copied link would add unintended (possibly dangling) graph edges to the report
    note; the link target survives as plain text, the brackets do not.
    """

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: campaign-a\ntype: campaign\n---\n"
            "See [[reaction-b]] for the esterification.\n",
            encoding="utf-8",
        )
        hits = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})
        assert hits[0].content == "See reaction-b for the esterification."
        assert "[[" not in hits[0].content

    asyncio.run(_run())


def test_fingerprint_retriever_cites_reaction_notes() -> None:
    """The fingerprint retriever cites reaction notes for structurally similar reactions."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for_reaction("eln-1", _ESTER))
        retriever = FingerprintReactionRetriever(store)

        hits = await retriever.retrieve(_ESTER, {})
        assert hits[0].source_note_id == "reaction-eln-1"  # cites the reaction note
        # A prose (non-reaction-SMILES) query yields no evidence, not an error.
        assert await retriever.retrieve("what was the yield?", {}) == []

    asyncio.run(_run())
