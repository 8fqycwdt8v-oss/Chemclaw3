"""One bad note must cost one note, not the whole derived index.

`reindex_notes` writes both the vector and the lexical legs, and its return value counts only
successes, so two silent failures are pinned: a note that briefly fails to parse is kept rather
than retired, and a note the embedder refuses costs only its own batch. Both drive the real
`reindex_notes` against `InMemoryNoteIndex`.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.retrieval import vector_index
from chemclaw.retrieval.vector_index import InMemoryNoteIndex, note_embedding_key, reindex_notes

_NOTE = "---\nid: {id}\ntype: reaction\ncreated_by: human\n---\n\nCoupling run {id}, 82% yield.\n"


def _corpus(directory: Path, count: int) -> None:
    """`count` valid notes, all parseable."""
    for index in range(count):
        (directory / f"note-{index:03d}.md").write_text(
            _NOTE.format(id=f"note-{index:03d}"), encoding="utf-8"
        )


def test_a_note_that_stops_parsing_is_kept_in_the_index_rather_than_retired(
    tmp_path: Path,
) -> None:
    """Breaking 40 of 100 notes' frontmatter must retire none of them.

    Their rows are still valid and the files will parse again once a sync finishes.
    """
    index = InMemoryNoteIndex()
    _corpus(tmp_path, 100)
    assert asyncio.run(reindex_notes(index, notes_dir=str(tmp_path))) == 100
    indexed = len(asyncio.run(index.fingerprints(_key())))
    assert indexed == 100

    for broken in range(40):
        path = tmp_path / f"note-{broken:03d}.md"
        path.write_text("---\nid: [unclosed\n---\n\nbroken\n", encoding="utf-8")

    asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))
    survived = len(asyncio.run(index.fingerprints(_key())))
    assert survived == 100, (
        f"a transient parse failure retired {100 - survived} note(s) from the derived index; "
        "they are unreachable by the dense and lexical legs until re-embedded"
    )


def test_a_readme_is_not_reported_as_a_note_that_did_not_parse(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A README is not reported as a note that did not parse, while a broken note still is.

    `note_file_fingerprints` keys every `*.md`, so `knowledge/README.md` was a standing false
    positive that made a real alarm indistinguishable from the baseline.
    """
    index = InMemoryNoteIndex()
    _corpus(tmp_path, 3)
    (tmp_path / "README.md").write_text(
        "# Knowledge\n\nHow this tree is organised.\n", encoding="utf-8"
    )

    with caplog.at_level("WARNING", logger="chemclaw.retrieval.vector_index"):
        assert asyncio.run(reindex_notes(index, notes_dir=str(tmp_path))) == 3
    assert not [record for record in caplog.records if "did not parse" in record.message], (
        "a file with no frontmatter is not a note, so it never dropped out of anything"
    )

    (tmp_path / "note-001.md").write_text("---\nid: [unclosed\n---\n\nbroken\n", encoding="utf-8")
    caplog.clear()
    with caplog.at_level("WARNING", logger="chemclaw.retrieval.vector_index"):
        asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))
    said = [record for record in caplog.records if "did not parse" in record.message]
    assert said and "note-001" in said[0].getMessage(), (
        "the alarm must still fire for a note that really did stop parsing"
    )


def test_a_note_the_embedder_refuses_costs_its_own_batch_and_no_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notes in batches that already landed stay landed; the blast radius is one batch.

    This does not decide which notes an endpoint should accept; the next pass retries what is
    missing.
    """
    index = InMemoryNoteIndex()
    _corpus(tmp_path, settings.note_embed_batch_size * 3)
    calls = {"n": 0}
    # The canonical function, so the name is imported from where it is defined; the *patch*
    # still lands on `vector_index`, because that is the namespace `reindex_notes` resolves in.
    real = embed_texts

    def _refuse_the_third_batch(texts: list[str], *, cache: bool = True) -> list[list[float]]:
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("maximum context length is 8192 tokens")
        return real(texts, cache=cache)

    monkeypatch.setattr(vector_index, "embed_texts", _refuse_the_third_batch)
    with pytest.raises(ValueError, match="8192"):
        asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))

    landed = len(asyncio.run(index.fingerprints(_key())))
    assert landed == settings.note_embed_batch_size * 2, (
        f"a refused batch left {landed} notes indexed; the batches before it should have survived"
    )


def test_an_oversized_note_is_truncated_rather_than_sent_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedded text is bounded, so an ordinary long campaign note cannot refuse itself.

    24,000 characters is ~6k tokens against an 8,192-token window. Measured on the code this
    replaces, a 989 kB note raised and left the index empty.
    """
    index = InMemoryNoteIndex()
    body = "Coupling yield. " * 80_000
    (tmp_path / "huge.md").write_text(
        f"---\nid: huge\ntype: campaign\ncreated_by: human\n---\n\n{body}\n", encoding="utf-8"
    )
    (tmp_path / "small.md").write_text(_NOTE.format(id="small"), encoding="utf-8")

    seen: list[int] = []
    # The canonical function, so the name is imported from where it is defined; the *patch*
    # still lands on `vector_index`, because that is the namespace `reindex_notes` resolves in.
    real = embed_texts

    def _record(texts: list[str], *, cache: bool = True) -> list[list[float]]:
        seen.extend(len(text) for text in texts)
        return real(texts, cache=cache)

    monkeypatch.setattr(vector_index, "embed_texts", _record)
    assert asyncio.run(reindex_notes(index, notes_dir=str(tmp_path))) == 2

    assert max(seen) <= settings.note_embed_max_chars
    assert len(asyncio.run(index.fingerprints(_key()))) == 2


def _key() -> str:
    """The embedding identity the index stores rows under."""
    return note_embedding_key()
