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
