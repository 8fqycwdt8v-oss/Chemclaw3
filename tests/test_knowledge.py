"""Tests for the result→note bridge and the git submitter (plan step 2.8)."""

import asyncio
import subprocess
from pathlib import Path

import pytest

import workflows.knowledge as knowledge
from kg.git_submitter import GitNoteSubmitter
from kg.note import Note
from kg.pr_gate import NoteSubmission
from workflows.knowledge import note_from_qm_result, write_knowledge_node
from workflows.models import QMJobResult

_RESULT = QMJobResult(
    molecule_smiles="CCO",
    method="B3LYP",
    basis_set="def2-SVP",
    total_energy_hartree=-154.75,
    converged=True,
    requested_by="oid-42",
)


def test_note_from_qm_result_maps_fields() -> None:
    """The result becomes an agent job-result note linking to its compound."""
    note = note_from_qm_result(_RESULT)
    assert note.type == "job-result"
    assert note.created_by == "agent"
    assert note.compound_smiles == "CCO"
    assert note.source == "qm:oid-42"  # provenance carried
    assert note.id.startswith("job-")
    links = note.outgoing_links()
    assert len(links) == 1 and links[0].startswith("compound-")  # reachable by traversal


def test_write_knowledge_node_uses_the_pr_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity proposes the mapped note through the (fake) submitter."""
    captured: list[NoteSubmission] = []

    class _Fake:
        async def submit(self, submission: NoteSubmission) -> str:
            captured.append(submission)
            return f"pr://{submission.branch}"

    monkeypatch.setattr(knowledge, "_default_submitter", lambda: _Fake())
    ref = asyncio.run(write_knowledge_node(_RESULT))

    assert ref.startswith("pr://note/job-")
    assert captured[0].path.startswith("knowledge/job-result/job-")


def test_git_submitter_pushes_branch(tmp_path: Path) -> None:
    """GitNoteSubmitter branches off the base and pushes the note (local-git only)."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    for key, value in {"user.email": "t@example.com", "user.name": "t"}.items():
        subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True)

    note = Note(id="job-abc", type="job-result", created_by="agent", body="[[compound-x]]")
    submission = NoteSubmission(
        branch="note/job-abc",
        path="knowledge/job-result/job-abc.md",
        content="---\nid: job-abc\ntype: job-result\ncreated_by: agent\n---\nbody\n",
        title="Add job-result note: job-abc",
        body="review please",
    )
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    ref = asyncio.run(submitter.submit(submission))

    assert ref == "note/job-abc"
    remote_refs = subprocess.run(
        ["git", "-C", str(work), "ls-remote", "origin", "note/job-abc"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "note/job-abc" in remote_refs.stdout
    assert note.type == "job-result"  # sanity on the model used above
