"""Retrieval carries provenance, so a claim can be qualified by who authored its evidence.

`gather_evidence` chunks carry `created_by`, `source` and `confidence` like `NoteRef` does;
confidence as ranking alone does not tell the model a note is uncertain. This matters once an
agent-authored tier is readable.
"""

from pathlib import Path

import yaml

from chemclaw.agent.research_tools import EvidenceSweepWithRefusals
from chemclaw.core.config import settings
from chemclaw.evals.probe import ProbeSet
from chemclaw.kg.note import Note
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.retrievers import GraphRetriever
from chemclaw.retrieval.vector_index import InMemoryNoteIndex, reindex_notes


def _write(directory: Path, note: Note) -> None:
    """Persist a note as the frontmatter+body file the loader reads."""
    lines = [
        "---",
        f"id: {note.id}",
        f"type: {note.type}",
        f"created_by: {note.created_by}",
    ]
    if note.source is not None:
        lines.append(f"source: {note.source}")
    if note.confidence is not None:
        lines.append(f"confidence: {note.confidence}")
    lines += ["---", "", note.body]
    (directory / f"{note.id}.md").write_text("\n".join(lines), encoding="utf-8")


async def test_a_graph_chunk_carries_who_wrote_it_and_how_sure_they_were(tmp_path: Path) -> None:
    """The three fields the answer contract now reasons over, on the default retrieval path."""
    _write(
        tmp_path,
        Note(
            id="playbook-pd",
            type="playbook",
            created_by="agent",
            source="distilled from 6 campaigns",
            confidence=0.4,
            body="Pd(OAc)2 with SPhos tends to hold at low loading.",
        ),
    )
    chunks = await GraphRetriever(str(tmp_path)).retrieve("SPhos", {})
    assert len(chunks) == 1
    assert chunks[0].created_by == "agent"
    assert chunks[0].source == "distilled from 6 campaigns"
    assert chunks[0].confidence == 0.4


async def test_a_human_note_says_human(tmp_path: Path) -> None:
    """The distinction only means something if both sides of it are actually reported."""
    _write(
        tmp_path,
        Note(id="reaction-1", type="reaction", created_by="human", body="Ran SPhos at 2 mol%."),
    )
    chunks = await GraphRetriever(str(tmp_path)).retrieve("SPhos", {})
    assert chunks[0].created_by == "human"


async def test_provenance_is_not_a_filter(tmp_path: Path) -> None:
    """An agent-authored, low-confidence note is returned and qualified, never suppressed.

    Retrieval has no basis for deciding on the reader's behalf which note counts.
    """
    _write(tmp_path, Note(id="a", type="playbook", created_by="agent", confidence=0.1, body="XX"))
    _write(tmp_path, Note(id="b", type="playbook", created_by="human", confidence=1.0, body="XX"))
    chunks = await GraphRetriever(str(tmp_path)).retrieve("XX", {})
    assert {c.source_note_id for c in chunks} == {"a", "b"}
    # ...but the trusted one is ranked first, so it survives truncation.
    assert chunks[0].source_note_id == "b"


async def test_the_dense_index_path_carries_the_same_provenance(tmp_path: Path) -> None:
    """The dense index path carries the same provenance as the graph path.

    Both fuse into one list, so partial provenance would qualify a note depending on which retriever
    surfaced it.
    """
    from chemclaw.retrieval.retrievers import VectorRetriever

    _write(
        tmp_path,
        Note(
            id="playbook-pd",
            type="playbook",
            created_by="agent",
            confidence=0.3,
            body="Pd(OAc)2 with SPhos holds at low loading.",
        ),
    )
    index = InMemoryNoteIndex()
    await reindex_notes(index, notes_dir=str(tmp_path))
    retriever = VectorRetriever(index, notes_dir=str(tmp_path))

    chunks = await retriever.retrieve("SPhos at low palladium loading", {})
    assert [c.created_by for c in chunks] == ["agent"]
    assert [c.confidence for c in chunks] == [0.3]


def test_unestablished_authorship_is_empty_not_human() -> None:
    """Unestablished authorship is empty, not "human".

    A structural hit is a Tanimoto score with no author; defaulting to "human" would be a false
    claim.
    """
    bare = EvidenceChunk(
        content="Similar reaction (Tanimoto 0.82)", source_note_id="r", retriever="x"
    )
    assert bare.created_by == ""
    assert bare.confidence is None


async def test_an_excerpt_windows_on_the_matched_term_rather_than_the_head_of_the_body(
    tmp_path: Path,
) -> None:
    """An excerpt windows on the matched term rather than the head of the body.

    A note matches on its whole searchable text, so a head-only excerpt often hides why it was
    retrieved, which matters most for cited report bullets. Campaign notes keep their outcomes in a
    table at the end of the body.
    """
    preamble = "Acetylation of salicylic acid with acetic anhydride. " * 8
    _write(
        tmp_path,
        Note(
            id="rxn-aspirin-acetylation",
            type="reaction",
            created_by="human",
            body=f"{preamble}\n\nThe isolated yield was 87 percent after recrystallisation.",
        ),
    )
    (chunk,) = await GraphRetriever(str(tmp_path)).retrieve("yield", {})

    assert "yield" in chunk.content, (
        f"the cited excerpt does not contain the term that matched: {chunk.content!r}"
    )
    assert len(chunk.content) <= settings.note_excerpt_chars


async def test_an_excerpt_with_no_body_match_still_starts_at_the_beginning(tmp_path: Path) -> None:
    """The control: a note matched on its id, type, tags or SMILES has no body offset to centre on.

    Windowing on nothing would be windowing on the first character anyway, so the head is the
    honest fallback rather than a special case — and it is what every excerpt was before.
    """
    body = "Charge the vessel, hold at 80 degrees, then work up into ethyl acetate. " * 6
    _write(tmp_path, Note(id="rxn-esterification", type="reaction", body=body))
    (chunk,) = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})

    assert chunk.content == body.strip()[: settings.note_excerpt_chars]


def test_the_conflict_marker_explains_itself_in_the_payload_the_model_reads() -> None:
    """The conflict marker explains itself in the payload the model reads.

    `conflicts_with`/`conflicts_total` are explained by a computed field on the chunk rather than in
    the tool description: the schema is near its per-tool token cap in
    `tests/test_context_floor.py`, and a computed field costs no prefix and appears only when there
    is something to warn about.
    """
    sweep = EvidenceSweepWithRefusals(
        chunks=[
            EvidenceChunk(
                content="Use 5 mol% Pd.",
                source_note_id="playbook-pd",
                retriever="graph",
                conflicts_with=["failure-1", "failure-2"],
                conflicts_total=2,
            )
        ]
    )
    assert "two independent confirmations" in sweep.disputed
    # The ids and the way to reach them: with the cap biting, they are the whole remaining trace.
    assert "failure-1" in sweep.disputed and "failure-2" in sweep.disputed
    assert "expand_note" in sweep.disputed
    # It reaches the model: a pydantic tool return arrives as its repr, and a plain property would
    # not be in it (`tests/test_upstream_surface.py` holds that shape).
    assert "disputed" in repr(sweep) and "disputed" in sweep.model_dump()


def test_the_marker_says_nothing_when_there_is_nothing_to_say() -> None:
    """The marker says nothing when there is nothing to say.

    A warning printed on every sweep is one the model learns to skip.
    """
    sweep = EvidenceSweepWithRefusals(
        chunks=[
            EvidenceChunk(
                content="Degas the solvent.", source_note_id="playbook-x", retriever="graph"
            )
        ]
    )
    assert sweep.disputed == ""


def test_the_corpus_grades_whether_a_disputed_note_is_qualified_in_the_answer() -> None:
    """The probe corpus grades whether a disputed note is qualified in the answer.

    Whether a model acts on the marker needs a live model, so the question lives in the eval corpus.
    Asserted by the claim the probe forbids rather than by its id, so renumbering does not read as
    lost coverage.
    """
    corpus = Path(__file__).resolve().parent.parent / "data" / "evals" / "probes" / "knowledge.yaml"
    probes = ProbeSet.model_validate(yaml.safe_load(corpus.read_text(encoding="utf-8"))).probes
    graded = [
        probe
        for probe in probes
        if any("independent confirmation" in claim for claim in probe.forbids_claims)
    ]
    assert graded, (
        "no probe grades whether an answer qualifies a note the sweep marked as disputed; "
        "`conflicts_with` is then a flag whose effect on an answer is unmeasured"
    )
    # The marker is what the answer has to survive on when the cap cut its disputers, so the
    # direction has to name the field rather than only the notes.
    assert any("conflicts_with" in probe.direction for probe in graded)


async def test_the_window_follows_the_term_that_points_somewhere_not_the_first_one_it_finds(
    tmp_path: Path,
) -> None:
    """The window follows the term that points somewhere, not the first one found.

    A framing word in the title would pin the excerpt to the head. The window maximises query
    coverage weighted by how much each term narrows this note: a term appearing once points at a
    place, one appearing six times points nowhere.
    """
    # `coupling` eight times in the head, `protodeboronation` once at the end: the earliest
    # match is in the first line, the answer is not.
    head = "Coupling notes on the coupling of the coupling partners. " * 5
    _write(
        tmp_path,
        Note(
            id="rxn-biaryl-run",
            type="reaction",
            created_by="human",
            body=(
                f"{head}\n\nThe low run failed by competitive "
                "protodeboronation of the boronic acid."
            ),
        ),
    )
    (chunk,) = await GraphRetriever(str(tmp_path)).retrieve(
        "did the coupling fail by protodeboronation", {}
    )

    assert "protodeboronation" in chunk.content, (
        f"the excerpt shows the framing term and not the one carrying the answer: {chunk.content!r}"
    )
    assert len(chunk.content) <= settings.note_excerpt_chars
