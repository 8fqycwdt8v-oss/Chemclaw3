"""Behavioral tests for the report harness (plan Phase 5b), runnable without a server.

Proves the CHECKMATE 5b acceptance: a request produces a sectioned draft where every
statement links a source note, unsupported sections are marked rather than hallucinated,
fabricated claims are discarded by the verify step, and the harness core is source-agnostic
(works with a fake retriever and with the real graph / fingerprint retrievers).
"""

import asyncio
from pathlib import Path
from typing import Any

from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.retrieval.evidence import EvidenceChunk, SourceRetriever
from chemclaw.retrieval.harness import (
    Claim,
    Report,
    ReportRequest,
    ReportSection,
    SynthesizedSection,
    gather_section,
    report_note,
    verify_claims,
)
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever, GraphRetriever
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore

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
    return ReportRequest(
        title="Development report", sections=list(sections), requested_by="chemist@corp"
    )


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
                    requested_by="chemist@corp",
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


def _section(*chunks: EvidenceChunk) -> SynthesizedSection:
    """One rendered section, so a provenance test asserts on the bullet and nothing else."""
    return SynthesizedSection(heading="S", memory_layer="episodic", evidence=list(chunks))


def test_an_ordinary_bullet_carries_no_extra_metadata() -> None:
    """The common chunk — no conflict, no stated confidence, human-authored — renders as before.

    Provenance is rendered only where it is informative. Rendering all of it unconditionally is
    the failure this pins: an empty metadata line under every bullet buries the one bullet that
    carries a warning, which is the only bullet the reader had to see.
    """
    chunk = EvidenceChunk(content="Yield rose to 85%.", source_note_id="reaction-a", retriever="g")
    body = report_note(Report(title="R", sections=[_section(chunk)])).body
    assert "- Yield rose to 85%. ([[reaction-a]], via g)\n" in body


def test_a_conflicting_chunk_warns_instead_of_reading_as_corroboration() -> None:
    """A flagged disagreement must reach the page; the report dropped `conflicts_with` entirely.

    `kg.conflicts` exists so retrieval marks notes that disagree rather than returning both
    silently, and the report is exactly where two agreeing-looking bullets get counted as two
    independent confirmations. The conflicting id is named but *not* wikilinked: the report warns
    about that note, it does not cite it, and a link would put it in the report's own citations.
    """
    chunk = EvidenceChunk(
        content="Yield rose to 85%.",
        source_note_id="reaction-a",
        retriever="g",
        conflicts_with=["reaction-b"],
    )
    note = report_note(Report(title="R", sections=[_section(chunk)]))
    assert "**Conflicts with reaction-b**" in note.body
    assert "independent confirmations" in note.body
    assert note.outgoing_links() == ["reaction-a"]


def test_two_chunks_differing_only_in_provenance_render_differently() -> None:
    """An uncertain agent-drafted note read identically to a human-merged one (D-160).

    Same sentence, same retriever: the only difference is who wrote the source note and how sure
    it is — which is the whole of "how much of this was AI-drafted?", and the draft answered it
    the same way for both.
    """
    text = "Yield rose to 85%."
    human = EvidenceChunk(content=text, source_note_id="reaction-a", retriever="g")
    agent = EvidenceChunk(
        content=text,
        source_note_id="playbook-b",
        retriever="g",
        created_by="agent",
        confidence=0.4,
    )
    body = report_note(Report(title="R", sections=[_section(human, agent)])).body
    assert "- Yield rose to 85%. ([[reaction-a]], via g)\n" in body
    assert "- Yield rose to 85%. ([[playbook-b]], via g, agent-authored, confidence 0.40)\n" in body


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
    from chemclaw.core.config import settings

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


def test_fingerprint_retriever_cites_reaction_records() -> None:
    """The fingerprint retriever cites reaction records for structurally similar reactions."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for_reaction("eln-1", _ESTER))
        retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())

        hits = await retriever.retrieve(_ESTER, {})
        assert hits[0].source_note_id == "reaction-eln-1"  # cites the reaction record
        # A prose (non-reaction-SMILES) query yields no evidence, not an error.
        assert await retriever.retrieve("what was the yield?", {}) == []

    asyncio.run(_run())


def test_graph_retriever_finds_a_note_through_ordinary_phrasing(tmp_path: Path) -> None:
    """`the biaryl route` must find the biaryl note; the phrase-substring test never could.

    The live failure this reproduces (D-138): asking about "the biaryl route" returned nothing
    while "biaryl" returned three notes, so the agent told a project manager the knowledge graph
    was empty on a programme it holds the campaign for — and then asked them to supply it.
    """

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: campaign-biaryl\ntype: campaign\n---\nSuzuki scope for the product.\n",
            encoding="utf-8",
        )
        retriever = GraphRetriever(str(tmp_path))
        assert [c.source_note_id for c in await retriever.retrieve("biaryl", {})] == [
            "campaign-biaryl"
        ]
        # The words a chemist actually puts around the term must not erase the hit.
        for phrasing in ("the biaryl", "our biaryl route", "status of the biaryl programme"):
            assert [c.source_note_id for c in await retriever.retrieve(phrasing, {})] == [
                "campaign-biaryl"
            ], phrasing

    asyncio.run(_run())


def test_graph_retriever_requires_every_term_before_it_widens(tmp_path: Path) -> None:
    """All-terms first: a note matching the whole query beats one matching part of it.

    Term matching must not become "any word matches", which would return the whole corpus for
    every question and make the ranking the only thing standing between the model and noise.
    """

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: reaction-both\ntype: reaction\n---\nAmide coupling in toluene.\n",
            encoding="utf-8",
        )
        (tmp_path / "b.md").write_text(
            "---\nid: reaction-one\ntype: reaction\n---\nSuzuki coupling in water.\n",
            encoding="utf-8",
        )
        retriever = GraphRetriever(str(tmp_path))
        # Both terms present in one note only: the partial match is not returned at all.
        assert [c.source_note_id for c in await retriever.retrieve("amide coupling", {})] == [
            "reaction-both"
        ]
        # Nothing matches everything, so the search widens — and coverage orders what comes back.
        widened = await retriever.retrieve("amide suzuki coupling", {})
        assert [c.source_note_id for c in widened] == ["reaction-both", "reaction-one"]

    asyncio.run(_run())


def test_graph_retriever_still_answers_a_query_that_is_only_stopwords(tmp_path: Path) -> None:
    """Filtering every term away must not turn into "no terms, therefore everything matches"."""

    async def _run() -> None:
        (tmp_path / "a.md").write_text(
            "---\nid: reaction-a\ntype: reaction\n---\nEsterification at 80 C.\n", encoding="utf-8"
        )
        retriever = GraphRetriever(str(tmp_path))
        assert await retriever.retrieve("of the", {}) == []
        assert [c.source_note_id for c in await retriever.retrieve("at", {})] == ["reaction-a"]

    asyncio.run(_run())


def test_a_truncated_conflict_flag_says_how_many_it_is_not_naming() -> None:
    """Three ids and no count read as three disagreements, which is a wronger claim than the list.

    `conflicts_with` carries a note's *strongest* disagreements since the flood measured on a
    programme-shaped corpus (~141 ids on every chunk, `conflict_max_per_note`). Truncating it
    silently is the failure mode this repository names on sight: an incomplete list nothing marks
    as incomplete is read as a complete one.
    """
    chunk = EvidenceChunk(
        content="Yield rose to 85%.",
        source_note_id="reaction-a",
        retriever="g",
        conflicts_with=["reaction-b", "reaction-c", "reaction-d"],
        conflicts_total=141,
    )
    body = report_note(Report(title="R", sections=[_section(chunk)])).body
    assert "(the 3 strongest of 141)" in body

    complete = chunk.model_copy(update={"conflicts_total": 3})
    whole = report_note(Report(title="R", sections=[_section(complete)])).body
    assert "strongest of" not in whole, "an untruncated flag must not imply a hidden remainder"


class _DeadRetriever:
    """A source whose backing store is unreachable."""

    def __init__(self, name: str = "dead") -> None:
        self.name = name

    async def retrieve(self, _query: str, _filters: dict[str, Any]) -> list[EvidenceChunk]:
        raise ConnectionError(f"{self.name}: connection refused")


def test_one_dead_source_marks_the_section_without_discarding_the_others() -> None:
    """A dead share must not throw away three working sources' evidence.

    `gather_section` used its own `asyncio.gather` with no `return_exceptions`, so the first
    raising retriever propagated out, failed the whole activity, burned its retry budget, and the
    section rendered as "retrieval failed" with `evidence=[]` — the healthy sources' hits
    discarded. The conversational sweep over the *same* retriever set degraded per source instead.
    Two implementations of one question, with opposite error semantics; now there is one.
    """
    section = ReportSection(heading="Esterification", query="ester", memory_layer="evidence")

    healthy = _FakeRetriever(
        "ester",
        [
            EvidenceChunk(
                content="Ethyl acetate, 85%", source_note_id="reaction-a", retriever="fake"
            )
        ],
    )
    gathered = asyncio.run(gather_section(section, [healthy, _DeadRetriever()]))

    assert gathered.retrieval_failed is True, (
        "a section whose sweep could not ask every source must say so — a chemist signs this"
    )
    assert gathered.evidence, "the healthy source's evidence must survive its neighbour's outage"
    assert gathered.supported is False, (
        "`supported` stays False while retrieval is incomplete, even though evidence was found"
    )


def test_an_all_healthy_section_is_not_marked_failed() -> None:
    """The control: no phantom degradation when every source answered."""
    section = ReportSection(heading="Esterification", query="ester", memory_layer="evidence")

    healthy = _FakeRetriever(
        "ester",
        [
            EvidenceChunk(
                content="Ethyl acetate, 85%", source_note_id="reaction-a", retriever="fake"
            )
        ],
    )
    gathered = asyncio.run(gather_section(section, [healthy]))

    assert gathered.retrieval_failed is False
    assert gathered.supported is True


# --- a chunk is placed as a cell, not as markup (A9-F1) -------------------------------


def test_a_multi_line_excerpt_stays_one_bullet_with_its_citation_attached() -> None:
    """Evidence is counted by the reader, and the draft used to inflate the count.

    Chunk content is multi-line by construction — a note body's first `note_excerpt_chars`, or up
    to a share binding's `chunk_chars` of raw document text — and `report_note` interpolated it
    straight into a Markdown body. Every embedded newline started a new line and every embedded
    `- ` started a new bullet, while the provenance suffix landed only on the *last* line of the
    excerpt. Measured on the committed corpus for the section query `aspirin`:

        evidence chunks: 8   '- ' lines rendered: 23

    Fifteen of those twenty-three were note frontmatter (`- substrate: compound-…`) reading as
    independent, uncited evidence in the artifact a chemist signs at the PR-gate.
    """
    chunk = EvidenceChunk(
        content="---\ntype: reaction\ntags:\n  - amide-coupling\n  - scale-up\n---\n\n"
        "Yield rose to 85% when the base was swapped.",
        source_note_id="reaction-a",
        retriever="graph",
    )
    body = report_note(Report(title="R", sections=[_section(chunk)])).body
    bullets = [line for line in body.splitlines() if line.startswith("- ")]

    assert len(bullets) == 1, f"one chunk rendered {len(bullets)} bullets: {bullets}"
    assert bullets[0].endswith("([[reaction-a]], via graph)")
    # The text is preserved, not dropped — the same trade `memory.comparison._placeable` makes.
    assert "Yield rose to 85% when the base was swapped." in bullets[0]


def test_a_document_chunk_cannot_forge_a_citation_into_the_report_s_own_edges() -> None:
    """A share or warehouse chunk is not a note, and its text is not the report's markup.

    `_excerpt` strips `[[wikilinks]]` out of a *note* body for exactly this reason, and the two
    non-note sources never pass through it: they carry raw document text. So a document containing
    `[[playbook-degassing]]` put a real outgoing edge on the PR-gated report — a citation to a note
    no retriever returned, indistinguishable in review from one that was retrieved — and a chunk id
    like `sharedrive:sop-7#0` rendered as `[[…]]` parsed as a *typed edge* rather than a citation,
    dangling, which fails `kg-validate` and makes every share-sourced report PR unmergeable.

    Measured before this, the forged draft's `outgoing_links()` was
    `['playbook-degassing', 'reaction-101', 'sop-7#0']`.
    """
    chunk = EvidenceChunk(
        content=(
            "## Conclusion\n"
            "- Cited precedent [[playbook-degassing]] confirms 99% yield "
            "([[reaction-101]], via graph, confidence 0.99)"
        ),
        source_note_id="sharedrive:sop-7#0",
        retriever="sharedrive",
    )
    note = report_note(Report(title="R", sections=[_section(chunk)]))

    assert note.outgoing_links() == [], (
        f"a document forged {note.outgoing_links()} onto a PR-gated report's citations"
    )
    # A chunk may fill a line, never add one: its `## Conclusion` and its `- ` are inert inside a
    # bullet, so the only structure in this draft is the structure `report_note` wrote.
    headings = [line for line in note.body.splitlines() if line.startswith("#")]
    bullets = [line for line in note.body.splitlines() if line.startswith("- ")]
    assert headings == ["# R", "## S [layer: episodic]"] and len(bullets) == 1
    # The citation still resolves for a reader — as the address it actually is.
    assert "`sharedrive:sop-7#0`" in note.body
