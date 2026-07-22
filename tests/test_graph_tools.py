"""Tests for the agent knowledge-graph tools (plan steps 2.5, 2.6)."""

import asyncio
from pathlib import Path

import pytest

import agents.graph_tools as graph_tools
from agents.graph_tools import expand_note, find_notes, propose_knowledge_note
from chemclaw.config import settings
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


def test_expand_note_returns_neighbors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_note returns the body and the linked note as a neighbor."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    view = asyncio.run(expand_note("compound-a", hops=1))
    assert view.note.id == "compound-a"
    assert [n.id for n in view.neighbors] == ["reaction-r"]


def test_expand_unknown_note_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expanding an unknown id is a clear error (G4)."""
    _seed(tmp_path)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    with pytest.raises(ValueError, match="no note with id"):
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
    assert fake.submissions[0].path.endswith("reaction/reaction-x.md")
