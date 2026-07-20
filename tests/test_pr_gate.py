"""Tests for note rendering and the PR-gate (plan steps 2.6, 2.7)."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from kg.note import Note, parse_note
from kg.pr_gate import NoteSubmission, propose_note
from kg.render import render_note


class _FakeSubmitter:
    """Records submissions instead of touching git, and returns a stub PR ref."""

    def __init__(self) -> None:
        self.submissions: list[NoteSubmission] = []

    async def submit(self, submission: NoteSubmission) -> str:
        self.submissions.append(submission)
        return f"pr://{submission.branch}"


def test_render_round_trips(tmp_path: Path) -> None:
    """render_note → file → parse_note preserves every field and link."""
    note = Note(
        id="compound-aspirin",
        type="compound",
        compound_smiles="CC(=O)Oc1ccccc1C(=O)O",
        tags=["nsaid"],
        created_by="agent",
        confidence=0.8,
        valid_from=date(2026, 1, 1),
        body="Made from [[compound-salicylic-acid]] via [[reaction-acetylation]].",
    )
    path = tmp_path / "note.md"
    path.write_text(render_note(note), encoding="utf-8")
    parsed = parse_note(path)
    assert parsed.model_dump(exclude={"body"}) == note.model_dump(exclude={"body"})
    assert parsed.outgoing_links() == note.outgoing_links()


def test_gate_submits_agent_note() -> None:
    """An agent note is laid out by type/id on its own branch with a review PR body."""
    note = Note(
        id="job-123",
        type="job-result",
        created_by="agent",
        source="qm",
        body="Energy computed for [[compound-x]].",
    )
    fake = _FakeSubmitter()
    ref = asyncio.run(propose_note(note, fake, knowledge_dir="knowledge"))

    assert ref == "pr://note/job-123"
    submission = fake.submissions[0]
    assert submission.branch == "note/job-123"
    assert submission.path == "knowledge/job-result/job-123.md"
    assert "job-123" in submission.title
    assert "human review" in submission.body.lower()
    assert "qm" in submission.body  # provenance carried into the PR body


def test_gate_rejects_human_note() -> None:
    """Human-authored notes are committed directly, not gated (G6/D-005)."""
    note = Note(id="manual", type="compound")  # created_by defaults to human
    with pytest.raises(ValueError, match="agent-authored"):
        asyncio.run(propose_note(note, _FakeSubmitter()))
