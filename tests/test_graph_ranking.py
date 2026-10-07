"""The graph leg ranks by relevance, not by note id.

`GraphRetriever` is the default text retriever, so its top `retrieval_top_k` is all a question
retrieves. Coverage ties whenever every hit matches every term, and `confidence` is a trust signal
that ties often, so a ranking on those falls through to the id. The tests assert outcomes ("the
note that answers is returned"), so a better ranker than BM25-lite passes unchanged.
"""

import asyncio
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.retrieval.retrievers import GraphRetriever

# What a note file needs to load. Deliberately identical for every fixture note except the body and
# the id, so nothing but the text can explain a difference in rank.
_FRONTMATTER = "---\nid: {id}\ntype: reaction\nconfidence: 0.85\ncreated_by: human\n---\n\n{body}\n"

# A note that answers "coupling yield", and 250 that mention both words once in passing. The
# answering ids sort *last* and the noise ids sort *first*, so an alphabetical ranking cannot
# accidentally pass.
_ANSWERS = {
    "zzz-answer-base": (
        "Coupling yield study: coupling yield rose with base loading. Yield 92% at 2.0 equiv. "
        "Coupling yield is base-limited; yield plateaus above 2.5 equiv."
    ),
    "zzz-answer-oxygen": (
        "Coupling yield collapsed under oxygen. Yield 12%. Degassing restored coupling yield "
        "to 88%. Yield is oxygen-sensitive."
    ),
    "zzz-answer-water": (
        "Coupling yield versus water content. Yield fell from 90% to 41%. Coupling yield "
        "tolerates 200 ppm water; yield drops sharply beyond."
    ),
}
_NOISE_BODY = "Weekly log. One coupling was run. The yield was recorded."


def _corpus(directory: Path, noise: int = 250) -> None:
    """Write the fixture: `noise` passing mentions, plus the three notes that answer it."""
    for index in range(noise):
        note_id = f"aaa-run-{index:03d}"
        (directory / f"{note_id}.md").write_text(
            _FRONTMATTER.format(id=note_id, body=_NOISE_BODY), encoding="utf-8"
        )
    for note_id, body in _ANSWERS.items():
        (directory / f"{note_id}.md").write_text(
            _FRONTMATTER.format(id=note_id, body=body), encoding="utf-8"
        )


def test_the_notes_that_answer_the_question_survive_the_cut(tmp_path: Path) -> None:
    """All three answering notes are returned from a corpus of 253 that all match every term.

    With `retrieval_top_k` at 8, which notes are discarded is all the ranker decides.
    """
    _corpus(tmp_path)
    chunks = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("coupling yield", {}))
    returned = [chunk.source_note_id for chunk in chunks]
    assert len(returned) == settings.retrieval_top_k
    assert set(_ANSWERS) <= set(returned), (
        f"the notes that answer the question were cut; got {returned}"
    )


def test_a_note_about_the_query_outranks_one_that_merely_mentions_it(tmp_path: Path) -> None:
    """The answering notes lead the list, rather than merely appearing in it.

    Rank matters beyond membership: this list is what RRF fuses and what the merge budget truncates
    downstream, so a chunk's position decides whether it survives the *next* two cuts as well.
    """
    _corpus(tmp_path)
    chunks = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("coupling yield", {}))
    leading = [chunk.source_note_id for chunk in chunks[: len(_ANSWERS)]]
    assert set(leading) == set(_ANSWERS), f"expected the answers to lead, got {leading}"


def test_a_rarer_term_carries_more_weight_than_a_common_one(tmp_path: Path) -> None:
    """A note matching the query's informative term beats one matching its ubiquitous one.

    Coverage ties at 1 and confidence is equal, so only inverse document frequency separates them.
    """
    for index in range(60):
        note_id = f"filler-{index:03d}"
        (tmp_path / f"{note_id}.md").write_text(
            _FRONTMATTER.format(id=note_id, body="Routine coupling performed."), encoding="utf-8"
        )
    (tmp_path / "common-only.md").write_text(
        _FRONTMATTER.format(id="common-only", body="A coupling was performed as usual."),
        encoding="utf-8",
    )
    (tmp_path / "zzz-rare-term.md").write_text(
        _FRONTMATTER.format(id="zzz-rare-term", body="Atropisomerism was observed on standing."),
        encoding="utf-8",
    )
    chunks = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("coupling atropisomerism", {}))
    returned = [chunk.source_note_id for chunk in chunks]
    assert returned[0] == "zzz-rare-term", f"the rare term did not lead; got {returned[:3]}"


def test_trust_still_breaks_a_tie_between_equally_relevant_notes(tmp_path: Path) -> None:
    """`confidence` still breaks a tie between equally relevant notes.

    The better-attested note survives truncation when relevance is equal.
    """
    body = "Coupling yield rose with base loading, then plateaued."
    for note_id, confidence in (("aaa-doubted", 0.4), ("zzz-trusted", 0.95)):
        (tmp_path / f"{note_id}.md").write_text(
            f"---\nid: {note_id}\ntype: reaction\nconfidence: {confidence}\n"
            f"created_by: human\n---\n\n{body}\n",
            encoding="utf-8",
        )
    chunks = asyncio.run(GraphRetriever(str(tmp_path)).retrieve("coupling yield", {}))
    assert [chunk.source_note_id for chunk in chunks] == ["zzz-trusted", "aaa-doubted"]
