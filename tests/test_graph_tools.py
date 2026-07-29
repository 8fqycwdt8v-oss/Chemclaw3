"""Tests for the agent knowledge-graph tools (plan steps 2.5, 2.6)."""

import asyncio
from pathlib import Path

import pytest

import chemclaw.agent.graph_tools as graph_tools
from chemclaw.agent.graph_tools import expand_note, find_notes, propose_knowledge_note
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from tests.conftest import FakeSubmitter


def _seed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\nid: compound-a\ntype: compound\ntags: [target]\n---\nMakes [[reaction-r]].\n",
        encoding="utf-8",
    )
    (tmp_path / "r.md").write_text(
        "---\nid: reaction-r\ntype: reaction\n---\nYields [[compound-a]].\n", encoding="utf-8"
    )


def test_find_notes_matches_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """find_notes locates a note by tag substring."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    refs = asyncio.run(find_notes("target"))
    assert {r.id for r in refs} == {"compound-a"}


def test_find_notes_matches_all_words_not_a_literal_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every word in the query must appear somewhere in the note — not as one exact phrase.

    Regression guard: a natural multi-word question ("target reaction") used to require that
    exact run of text to appear verbatim, so it missed a note whose words are present but not
    adjacent in that order — a real live-e2e finding where the model then reported "no data"
    even though the corpus had it.
    """
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    # "target" is on compound-a; "reaction" only appears on reaction-r's own id/type, and
    # compound-a's body only links to it as "[[reaction-r]]" — no note contains the literal
    # phrase "target reaction", but compound-a contains both words independently.
    refs = asyncio.run(find_notes("target reaction"))
    assert {r.id for r in refs} == {"compound-a"}


def test_find_notes_returns_nothing_when_one_word_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-words matching still excludes a note missing even one of the query's words."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    assert asyncio.run(find_notes("target nonexistentword")) == []


def test_expand_note_returns_neighbors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_note returns the body and the linked note as a neighbor."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("compound-a", hops=1))
    assert view.note.id == "compound-a"
    assert [n.id for n in view.neighbors] == ["reaction-r"]


def test_expand_unknown_note_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanding an unknown id is a clear error (G4), and a `ChemclawError` specifically.

    `ChemclawError` (a `ValueError` subclass) is chemclaw's own always-safe "bad input"
    contract, so `chemclaw.agent.tool_authz.surface_domain_errors` surfaces this message to the
    model
    verbatim instead of MAF's opaque generic failure — the common real cause is a citation to a
    note still pending PR-gate review, which the chemist can otherwise not distinguish from a
    typo or a deleted note.
    """
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    with pytest.raises(ChemclawError, match="no note with id"):
        asyncio.run(expand_note("ghost"))


def test_expand_note_clamps_hops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A huge `hops` is clamped to the configured max, not traversed unbounded (SEC-4)."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_hops", 2)
    # An absurd hop count returns the same bounded neighborhood as the max, never errors or hangs.
    huge = asyncio.run(expand_note("compound-a", hops=10_000))
    at_max = asyncio.run(expand_note("compound-a", hops=2))
    assert {n.id for n in huge.neighbors} == {n.id for n in at_max.neighbors}


def test_find_notes_surfaces_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NoteRef carries provenance (author/source/confidence) so the agent can weigh it (KM-6)."""
    (tmp_path / "p.md").write_text(
        "---\nid: reaction-p\ntype: reaction\ncreated_by: agent\nsource: eln-7\n"
        "confidence: 0.8\n---\nA [[compound-a]] prep.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    (ref,) = asyncio.run(find_notes("prep"))
    assert ref.created_by == "agent"
    assert ref.source == "eln-7"
    assert ref.confidence == 0.8


def test_find_notes_excludes_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired note (valid_to in the past) is not surfaced as current evidence (KM-7)."""
    (tmp_path / "old.md").write_text(
        "---\nid: reaction-old\ntype: reaction\nvalid_to: 2000-01-01\ntags: [reflux]\n---\nOld.\n",
        encoding="utf-8",
    )
    (tmp_path / "new.md").write_text(
        "---\nid: reaction-new\ntype: reaction\ntags: [reflux]\n---\nCurrent.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    refs = asyncio.run(find_notes("reflux"))
    assert {r.id for r in refs} == {"reaction-new"}  # the expired note is dropped


def test_expand_note_drops_expired_neighbor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anchor is returned by explicit id, but an expired neighbor is filtered out (KM-7)."""
    (tmp_path / "a.md").write_text(
        "---\nid: compound-a\ntype: compound\n---\nMakes [[reaction-old]] and [[reaction-r]].\n",
        encoding="utf-8",
    )
    (tmp_path / "old.md").write_text(
        "---\nid: reaction-old\ntype: reaction\nvalid_to: 2000-01-01\n---\nExpired.\n",
        encoding="utf-8",
    )
    (tmp_path / "r.md").write_text(
        "---\nid: reaction-r\ntype: reaction\n---\nCurrent.\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("compound-a", hops=1))
    assert [n.id for n in view.neighbors] == ["reaction-r"]  # expired neighbor excluded


def test_find_notes_caps_the_hit_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broad needle is truncated to the cap, in a stable order.

    Every hit lands in the model's context window, so an uncapped sweep over a real corpus is a
    context blowout. Truncation is by sorted id so the same query returns the same notes.
    """
    for i in range(10):
        (tmp_path / f"n{i:02d}.md").write_text(
            f"---\nid: reaction-{i:02d}\ntype: reaction\n---\nAn acetylation.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    monkeypatch.setattr(settings, "graph_max_results", 3)
    refs = asyncio.run(find_notes("acetylation"))
    assert [r.id for r in refs] == ["reaction-00", "reaction-01", "reaction-02"]
    assert [r.id for r in asyncio.run(find_notes("acetylation"))] == [r.id for r in refs]


def test_find_notes_warns_only_when_it_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hitting the cap warns (never a silent partial answer); a complete result stays quiet."""
    for i in range(4):
        (tmp_path / f"n{i}.md").write_text(
            f"---\nid: reaction-{i}\ntype: reaction\n---\nAn acetylation.\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))

    monkeypatch.setattr(settings, "graph_max_results", 2)
    with caplog.at_level("WARNING"):
        asyncio.run(find_notes("acetylation"))
    assert "find_notes capped at 2 matches" in caplog.text

    caplog.clear()
    monkeypatch.setattr(settings, "graph_max_results", 50)
    with caplog.at_level("WARNING"):
        asyncio.run(find_notes("acetylation"))
    assert "capped" not in caplog.text


def test_propose_knowledge_note_uses_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write tool proposes an agent note through the (fake) PR-gate."""
    fake = FakeSubmitter()
    monkeypatch.setattr(graph_tools, "default_submitter", lambda: fake)
    ref = asyncio.run(
        propose_knowledge_note(
            id="reaction-x", type="reaction", body="From [[compound-a]].", source="eln-1"
        )
    )
    assert ref == "pr://note/reaction-x"
    assert fake.submissions[0].files[0].path.endswith("reaction/reaction-x.md")
