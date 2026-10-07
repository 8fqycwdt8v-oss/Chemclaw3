"""Behavioral tests for the report harness, runnable without a server.

Every statement links a source note, unsupported sections are marked rather than invented,
fabricated claims are discarded by the verify step, and the harness core is source-agnostic.
"""

import asyncio
from pathlib import Path
from typing import Any

from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.retrieval.evidence import EvidenceChunk, RetrieverSkip, SourceRetriever
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

    def __init__(self, keyword: str, chunks: list[EvidenceChunk], name: str = "fake") -> None:
        self._keyword = keyword
        self._chunks = chunks
        self.name = name

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        return self._chunks if self._keyword in query else []


def _request(*sections: ReportSection) -> ReportRequest:
    return ReportRequest(
        title="Development report", sections=list(sections), requested_by="chemist@corp"
    )


# --- harness core (5b.1) --------------------------------------------------------------


async def test_gather_marks_unsupported_section_instead_of_inventing() -> None:
    """A section with no retrieved evidence is kept but marked unsupported (no hallucination)."""
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


async def test_report_note_cites_every_source() -> None:
    """Every evidence chunk in the draft wikilinks its source note (5b.7)."""
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
    """An ordinary chunk renders with no extra metadata line.

    Provenance renders only where informative, so a warning bullet is not buried among empty ones.
    """
    chunk = EvidenceChunk(content="Yield rose to 85%.", source_note_id="reaction-a", retriever="g")
    body = report_note(Report(title="R", sections=[_section(chunk)])).body
    assert "- Yield rose to 85%. ([[reaction-a]], via g)\n" in body


def test_a_conflicting_chunk_warns_instead_of_reading_as_corroboration() -> None:
    """A conflicting chunk warns instead of reading as corroboration.

    The conflicting id is named but not wikilinked, so it does not become one of the report's
    citations.
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
    """Two chunks differing only in authorship and confidence render differently."""
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


def test_a_document_citation_grounds_against_the_stored_chunk_id() -> None:
    """A document-chunk citation grounds against the stored `<retriever>:<doc>#<ordinal>` id.

    `cited_ids` splits at the first colon, so it is driven through `cited_ids` to test the real
    coupling with `groundable_ids`.
    """
    from chemclaw.kg.note import cited_ids

    evidence = [
        EvidenceChunk(content="the SOP says", source_note_id="docs:abc123#4", retriever="documents")
    ]
    citations = cited_ids("Per the SOP ([[docs:abc123#4]]).")
    claims = [Claim(text="Per the SOP.", citations=citations)]
    supported, discarded = verify_claims(claims, evidence)
    assert [c.text for c in supported] == ["Per the SOP."]
    assert discarded == []
    # A fabricated document citation still fails: the split half of a *different* id is no match.
    ghost = cited_ids("Per the SOP ([[docs:ffff99#1]]).")
    supported, discarded = verify_claims([Claim(text="Ghost.", citations=ghost)], evidence)
    assert supported == []


# --- concrete retrievers (5b.3) -------------------------------------------------------


async def test_graph_retriever_matches_and_cites_notes(tmp_path: Path) -> None:
    """The graph retriever returns citable chunks from notes matching the query + filters."""
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


async def test_graph_retriever_scores_by_confidence(tmp_path: Path) -> None:
    """Each chunk carries a score from its note's confidence, defaulting when absent (KM-5)."""
    from chemclaw.core.config import settings

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


async def test_graph_retriever_ranks_hits_by_score_not_disk_order(tmp_path: Path) -> None:
    """Graph hits come back best-first (KM-5), not in alphabetical file order (the RRF contract)."""
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


async def test_graph_retriever_excludes_expired_notes(tmp_path: Path) -> None:
    """A report never cites a note past its `valid_to` as current evidence (KM-7)."""
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


async def test_graph_retriever_excerpt_strips_wikilinks(tmp_path: Path) -> None:
    """An excerpt never carries a source note's `[[wikilink]]` into the report verbatim.

    A copied link would add unintended (possibly dangling) graph edges to the report
    note; the link target survives as plain text, the brackets do not.
    """
    (tmp_path / "a.md").write_text(
        "---\nid: campaign-a\ntype: campaign\n---\nSee [[reaction-b]] for the esterification.\n",
        encoding="utf-8",
    )
    hits = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})
    assert hits[0].content == "See reaction-b for the esterification."
    assert "[[" not in hits[0].content


async def test_fingerprint_retriever_cites_reaction_records() -> None:
    """The fingerprint retriever cites reaction records for structurally similar reactions."""
    store = InMemoryFingerprintStore()
    await store.add(record_for_reaction("eln-1", _ESTER))
    retriever = FingerprintReactionRetriever(store, InMemoryReactionRecordStore())

    hits = await retriever.retrieve(_ESTER, {})
    assert hits[0].source_note_id == "reaction-eln-1"  # cites the reaction record
    # A prose (non-reaction-SMILES) query yields no evidence, not an error.
    assert await retriever.retrieve("what was the yield?", {}) == []


async def test_graph_retriever_finds_a_note_through_ordinary_phrasing(tmp_path: Path) -> None:
    """`the biaryl route` must find the biaryl note through ordinary phrasing, not a substring
    match.
    """
    (tmp_path / "a.md").write_text(
        "---\nid: campaign-biaryl\ntype: campaign\n---\nSuzuki scope for the product.\n",
        encoding="utf-8",
    )
    retriever = GraphRetriever(str(tmp_path))
    assert [c.source_note_id for c in await retriever.retrieve("biaryl", {})] == ["campaign-biaryl"]
    # The words a chemist actually puts around the term must not erase the hit.
    for phrasing in ("the biaryl", "our biaryl route", "status of the biaryl programme"):
        assert [c.source_note_id for c in await retriever.retrieve(phrasing, {})] == [
            "campaign-biaryl"
        ], phrasing


async def test_graph_retriever_requires_every_term_before_it_widens(tmp_path: Path) -> None:
    """All-terms first: a note matching the whole query beats one matching part of it.

    Term matching must not become "any word matches", which would return the whole corpus for
    every question and make the ranking the only thing standing between the model and noise.
    """
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


async def test_graph_retriever_still_answers_a_query_that_is_only_stopwords(tmp_path: Path) -> None:
    """Filtering every term away must not turn into "no terms, therefore everything matches"."""
    (tmp_path / "a.md").write_text(
        "---\nid: reaction-a\ntype: reaction\n---\nEsterification at 80 C.\n", encoding="utf-8"
    )
    retriever = GraphRetriever(str(tmp_path))
    assert await retriever.retrieve("of the", {}) == []
    assert [c.source_note_id for c in await retriever.retrieve("at", {})] == ["reaction-a"]


def test_a_truncated_conflict_flag_says_how_many_it_is_not_naming() -> None:
    """A truncated conflict flag says how many it is not naming, so it is not read as complete."""
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
    """A dead source marks the section without discarding the other sources' evidence."""
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


def test_a_note_two_sources_both_found_is_one_bullet_not_two() -> None:
    """A note two sources both found is one bullet, not two.

    Text retrievers excerpt the same body, so concatenating per-source lists would cite one note
    repeatedly and read as independent evidence.
    """
    section = ReportSection(heading="Esterification", query="ester", memory_layer="evidence")
    excerpt = "Ethyl acetate, 85% after distillation."
    graph = _FakeRetriever(
        "ester",
        [EvidenceChunk(content=excerpt, source_note_id="reaction-a", retriever="graph")],
        name="graph",
    )
    vector = _FakeRetriever(
        "ester",
        [EvidenceChunk(content=excerpt, source_note_id="reaction-a", retriever="vector")],
        name="vector",
    )

    gathered = asyncio.run(gather_section(section, [graph, vector]))

    assert [chunk.retriever for chunk in gathered.evidence] == ["graph"], (
        "the first list's chunk represents the note, which is what argument order is for"
    )
    body = report_note(Report(title="R", sections=[gathered])).body
    assert len([line for line in body.splitlines() if line.startswith("- ")]) == 1


def test_two_different_excerpts_of_one_note_are_still_two_pieces_of_evidence() -> None:
    """The merge keys on `(note, content)`, not on the note — the round-robin's contract.

    A report has no budget cap to spend, so dropping a second, genuinely different excerpt would
    discard evidence to fix a repeat. Only a byte-identical repeat is dropped.
    """
    section = ReportSection(heading="Esterification", query="ester", memory_layer="evidence")
    graph = _FakeRetriever(
        "ester",
        [
            EvidenceChunk(
                content="85% after distillation.", source_note_id="rxn-a", retriever="graph"
            )
        ],
        name="graph",
    )
    share = _FakeRetriever(
        "ester",
        [
            EvidenceChunk(
                content="The SOP calls for a 6 h reflux.", source_note_id="rxn-a", retriever="share"
            )
        ],
        name="share",
    )

    gathered = asyncio.run(gather_section(section, [graph, share]))

    assert len(gathered.evidence) == 2


# --- a chunk is placed as a cell, not as markup (A9-F1) -------------------------------


def test_a_multi_line_excerpt_stays_one_bullet_with_its_citation_attached() -> None:
    """A multi-line excerpt stays one bullet with its citation attached.

    Embedded newlines and `- ` in chunk content would otherwise render as extra uncited bullets.
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
    """A document chunk cannot forge a citation into the report's own edges.

    Share and warehouse chunks are raw text, so their `[[wikilinks]]` are stripped and their ids are
    not rendered as links; otherwise the report gains unretrieved or dangling edges.
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


def test_a_partially_failed_section_renders_the_evidence_it_kept() -> None:
    """A partially failed section renders both the failure marker and the evidence it kept."""
    partial = SynthesizedSection(
        heading="Yield",
        memory_layer="evidence",
        evidence=[
            EvidenceChunk(
                content="Ethyl acetate, 85%", source_note_id="reaction-a", retriever="fake"
            )
        ],
        retrieval_failed=True,
    )
    text = report_note(Report(title="R", sections=[partial])).body
    assert "incomplete" in text, "the reviewer must still see the gap"
    assert "Ethyl acetate, 85%" in text, "the surviving sources' evidence must render"
    assert "No supporting data found" not in text


# --- a declined source and a broken one are two different facts -----------------------------------


class _RaisingRetriever:
    """A source whose backing store is down: a transient a re-run can fix."""

    name = "vector"

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Fail the way an unreachable index does."""
        raise ConnectionError("pgvector unreachable at 10.0.0.4:5432")


class _DecliningRetriever:
    """A source that refuses this actor: a re-run as the same actor gets the same refusal."""

    name = "share"

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Decline the way an unentitled share leg does."""
        raise RetrieverSkip("the service actor holds no entitlement for this share")


async def _one_section(retrievers: list[Any]) -> SynthesizedSection:
    """Sweep one section over `retrievers` through the real `gather_section`."""
    return await gather_section(
        ReportSection(heading="Yield", query="yield", memory_layer="episodic"), retrievers
    )


async def test_a_declined_source_and_a_failed_one_do_not_render_the_same_sentence() -> None:
    """A declined source and a failed one render different sentences, since their remedies differ.

    Re-running as the same actor cannot fix a declined (unentitled) source.
    """
    broken = await _one_section([_FakeRetriever("yield", _CHUNKS), _RaisingRetriever()])
    declined = await _one_section([_FakeRetriever("yield", _CHUNKS), _DecliningRetriever()])

    assert broken.retrieval_failed and declined.retrieval_failed
    assert broken.failed_sources == ["vector"] and broken.skipped_sources == {}
    assert declined.failed_sources == [] and list(declined.skipped_sources) == ["share"]

    broken_line = _marker(report_note(Report(title="R", sections=[broken])).body)
    declined_line = _marker(report_note(Report(title="R", sections=[declined])).body)

    assert broken_line != declined_line
    # The failure family keeps its opening words, which is the substring every earlier report
    # carries and a reader greps for.
    assert broken_line.startswith("_Retrieval failed for vector")
    assert "re-run required" in broken_line
    # The skip family says the opposite thing, because the opposite thing is true.
    assert "re-run required" not in declined_line
    assert "the entitlement or the filters must change" in declined_line
    # Both name the source. "Some retrieval sources" sends nobody anywhere.
    assert "share: the service actor holds no entitlement" in declined_line

    # And the evidence the working leg found is still rendered under either marker.
    for section in (broken, declined):
        assert [chunk.source_note_id for chunk in section.evidence] == ["reaction-a"]


def test_a_section_with_no_per_source_detail_renders_exactly_as_it_always_did() -> None:
    """A section with no per-source detail renders exactly as before.

    `durable/report_workflow.py` builds such sections when the whole activity failed, and existing
    reports carry that wording.
    """
    whole = SynthesizedSection(
        heading="Yield", memory_layer="episodic", evidence=[], retrieval_failed=True
    )
    assert whole.failed_sources == [] and whole.skipped_sources == {}
    assert (
        _marker(report_note(Report(title="R", sections=[whole])).body)
        == "_Retrieval failed for this section; incomplete — re-run required._"
    )


_CHUNKS = [
    EvidenceChunk(content="Pd/C at 40 psi gave 82%.", source_note_id="reaction-a", retriever="fake")
]


def _marker(body: str) -> str:
    """The one italic marker line a rendered section carries, for substring assertions."""
    return next(line for line in body.splitlines() if line.startswith("_"))
