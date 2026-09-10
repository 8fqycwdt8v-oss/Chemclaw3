"""Retrieval carries provenance, so a claim can be qualified by who authored its evidence (D-160).

`NoteRef` has exposed `created_by`, `source` and `confidence` to `find_notes`/`expand_note` since
KM-6. `gather_evidence` — the sweep that gathers most of the evidence an answer is actually built
on — carried none of them. Confidence *reached* the chunk, as `score`, which orders truncation:
being ranked lower is not the same as being told a note is uncertain, and the model was never told.

While everything readable is human-merged that is harmless. It stops being harmless the moment a
second, ungated tier exists, which is why this lands before that one and on its own.
"""

import asyncio
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


def test_a_graph_chunk_carries_who_wrote_it_and_how_sure_they_were(tmp_path: Path) -> None:
    """The three fields the answer contract now reasons over, on the default retrieval path."""

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_human_note_says_human(tmp_path: Path) -> None:
    """The distinction only means something if both sides of it are actually reported."""

    async def _run() -> None:
        _write(
            tmp_path,
            Note(id="reaction-1", type="reaction", created_by="human", body="Ran SPhos at 2 mol%."),
        )
        chunks = await GraphRetriever(str(tmp_path)).retrieve("SPhos", {})
        assert chunks[0].created_by == "human"

    asyncio.run(_run())


def test_provenance_is_not_a_filter(tmp_path: Path) -> None:
    """An agent-authored, low-confidence note is *returned* and qualified, never suppressed.

    Retrieval has no basis for deciding a merged note should not be seen — a human signed it off.
    Dropping it would be the same mistake `conflicts_with` was written to avoid: silently deciding
    on the reader's behalf which of two curated notes counts.
    """

    async def _run() -> None:
        _write(
            tmp_path, Note(id="a", type="playbook", created_by="agent", confidence=0.1, body="XX")
        )
        _write(
            tmp_path, Note(id="b", type="playbook", created_by="human", confidence=1.0, body="XX")
        )
        chunks = await GraphRetriever(str(tmp_path)).retrieve("XX", {})
        assert {c.source_note_id for c in chunks} == {"a", "b"}
        # ...but the trusted one is ranked first, so it survives truncation.
        assert chunks[0].source_note_id == "b"

    asyncio.run(_run())


def test_the_dense_index_path_carries_the_same_provenance(tmp_path: Path) -> None:
    """One builder feeds both paths, because a partially-provenanced list is the worst state.

    Two retrievers fuse into one evidence list. If the graph path reported authorship and the
    index path did not, an agent-authored note would be qualified or not depending on which
    retriever happened to surface it — indistinguishable, from the model's side, from a note that
    genuinely had no author.
    """
    from chemclaw.retrieval.retrievers import VectorRetriever

    async def _run() -> None:
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

    asyncio.run(_run())


def test_unestablished_authorship_is_empty_not_human() -> None:
    """The default must not assert provenance nobody checked.

    A structural hit is generated from the fingerprint index — its content is a Tanimoto score,
    not a sentence anyone wrote — so there is no author to report. Defaulting to "human" would put
    a false claim in the one field whose entire purpose is to be trusted.
    """
    bare = EvidenceChunk(
        content="Similar reaction (Tanimoto 0.82)", source_note_id="r", retriever="x"
    )
    assert bare.created_by == ""
    assert bare.confidence is None


def test_an_excerpt_windows_on_the_matched_term_rather_than_the_head_of_the_body(
    tmp_path: Path,
) -> None:
    """A reviewer must be able to see what a cited note was retrieved *for*.

    A note matches on its whole searchable text — id, type, SMILES, tags and body — while the chunk
    carried the first `note_excerpt_chars` of the body and nothing windowed it on the match.
    Measured over the committed corpus for the query `yield`: of 38 notes, 32 have bodies longer
    than 240 characters, and `16 chunks, 6 whose 240-char excerpt does NOT contain the matched
    term`. For the conversational tool that is recoverable with `expand_note`; for `report_note` it
    is the final artifact a chemist signs at the PR-gate, where a bullet of frontmatter and a
    citation says nothing about why the note is there.

    `campaign` and `optimization-campaign` are the worst case by construction — their yields,
    purities and outcomes are in a table at the *end* of the body — which is the same failure
    `core/config/retrieval.py` already articulates for `protocol_digest_max_chars`.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_an_excerpt_with_no_body_match_still_starts_at_the_beginning(tmp_path: Path) -> None:
    """The control: a note matched on its id, type, tags or SMILES has no body offset to centre on.

    Windowing on nothing would be windowing on the first character anyway, so the head is the
    honest fallback rather than a special case — and it is what every excerpt was before.
    """

    async def _run() -> None:
        body = "Charge the vessel, hold at 80 degrees, then work up into ethyl acetate. " * 6
        _write(tmp_path, Note(id="rxn-esterification", type="reaction", body=body))
        (chunk,) = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})

        assert chunk.content == body.strip()[: settings.note_excerpt_chars]

    asyncio.run(_run())


def test_the_conflict_marker_explains_itself_in_the_payload_the_model_reads() -> None:
    """A flag nothing explains is a flag nobody acts on.

    `conflicts_with`/`conflicts_total` ride on every chunk `gather_evidence` returns and were
    described nowhere the model reads: measured over the whole conversational path, "conflict",
    "contradict" and "disput" appear **zero** times in the assembled system prompt, zero times in
    the tool description, zero times in any `SKILL.md`, and `EvidenceChunk`'s nine fields all
    carried `description=None`. The report path has rendered the right sentence per chunk since the
    marker existed (`retrieval/harness.py`); the conversational path, which is every turn, had
    neither half.

    Asserted on the **payload** rather than on the tool description, and the reason is measured
    rather than stylistic: `gather_evidence`'s schema is 881 tokens against
    `tests/test_context_floor.py`'s 900-token per-tool cap, and the shortest honest version of this
    sentence cost 73 — 954, which that ratchet refuses. A computed field costs nothing in the
    prefix and is present only when there is something to warn about, which is the same argument
    `NoteSearch.verdict` makes.
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
    """The negative control, and the reason this is a computed field rather than prose.

    Most sweeps have no disagreement in them. A warning printed on all of them is a warning the
    model learns to skip, which is the failure mode a static `Returns:` sentence would have had —
    and would have paid 73 tokens of prefix per model call for.
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
    """Whether a *model* acts on the marker is a number, and nobody had it.

    The two tests above assert the mechanism: the flag rides on the chunk and the description now
    says what it means. Neither can say whether an answer built on a marked chunk actually carries
    the caveat, because that needs a live model and a gateway this sandbox does not have. So the
    corpus is where the question is parked — `data/evals/probes/reaction.yaml`'s `rx-33` already
    grades the same contradiction from the *optimisation* side (does a suggestion get checked
    against the record), and what was ungraded is the reader's side: a chunk arriving marked, with
    the notes that dispute it possibly cut by the cap.

    Asserted by the claim it forbids rather than by a probe id, so a renumbering of the corpus does
    not read as the coverage disappearing.
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


def test_the_window_follows_the_term_that_points_somewhere_not_the_first_one_it_finds(
    tmp_path: Path,
) -> None:
    """One matched term in the opening line used to pin the excerpt to the head.

    `_window_start` took `first = min(offsets)` — the earliest occurrence of *any* matched term —
    and then returned `0` whenever that offset was inside the budget. So a query whose framing
    word sits in the note's title got the title, and the term carrying the answer was never
    reached. Measured over the 19 independently-authored `knowledge.yaml` probes: of 30 delivered
    gold chunks whose body exceeds the 240-char budget, **25 were the plain head** and only 13
    showed every term that matched. The worst case is the one a chemist actually asks —
    `rxn-suzuki-biaryl` for "what isolated yield did the Suzuki coupling of 4-bromoanisole give"
    lost `isolated` and `yield`, which is the question *and* the 76% answer.

    The rule is now the window that shows the most of the query, weighted by how much each term
    narrows *this note*: a term appearing once points at a place, a term appearing six times
    points nowhere. Measured the same way that moved term visibility 84/110 → 92/110 and full
    coverage 13/30 → 17/30, with `isolated yield: 76%` inside the p01 excerpt.
    """

    async def _run() -> None:
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
            "the excerpt shows the framing term and not the one carrying the answer: "
            f"{chunk.content!r}"
        )
        assert len(chunk.content) <= settings.note_excerpt_chars

    asyncio.run(_run())
