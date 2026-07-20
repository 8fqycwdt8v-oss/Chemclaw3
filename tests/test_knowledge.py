"""Tests for the result→note bridge and the git submitter (plan step 2.8)."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.knowledge as knowledge
from chemclaw.config import settings
from kg.git_submitter import GitNoteSubmitter, GitSubmitError
from kg.note import Note
from kg.pr_gate import NoteSubmission
from tests.conftest import FakeSubmitter
from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip
from workflows.knowledge import note_from_qm_result, write_knowledge_node
from workflows.models import QMJobInput, QMJobResult
from workflows.qm_job import QMJobWorkflow

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
    # No dangling wikilink to a non-existent compound note (would fail kg-validate).
    assert note.outgoing_links() == []


def test_write_knowledge_node_uses_the_pr_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity proposes the mapped note through the (fake) submitter."""
    fake = FakeSubmitter()
    monkeypatch.setattr(knowledge, "default_submitter", lambda: fake)
    ref = asyncio.run(write_knowledge_node(_RESULT))

    assert ref.startswith("pr://note/job-")
    assert fake.submissions[0].path.startswith("knowledge/job-result/job-")


def _clone(remote: Path, dest: Path) -> Path:
    """Clone the bare remote and configure a committer identity."""
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True)
    for key, value in {"user.email": "t@example.com", "user.name": "t"}.items():
        subprocess.run(["git", "-C", str(dest), "config", key, value], check=True)
    return dest


def _make_remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'remote' with a seeded `main` branch, plus one working clone of it."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work = _clone(remote, tmp_path / "work")
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True)
    return remote, work


def _note_submission(note_id: str, content: str = "body\n") -> NoteSubmission:
    """A minimal job-result submission for `note_id` with the standard layout."""
    return NoteSubmission(
        branch=f"note/{note_id}",
        path=f"knowledge/job-result/{note_id}.md",
        content=content,
        title=f"Add job-result note: {note_id}",
        body="review please",
    )


def test_git_submitter_pushes_branch(tmp_path: Path) -> None:
    """GitNoteSubmitter branches off the base and pushes the note (local-git only)."""
    _, work = _make_remote_and_clone(tmp_path)

    note = Note(id="job-abc", type="job-result", created_by="agent", body="[[compound-x]]")
    submission = _note_submission(
        "job-abc", content="---\nid: job-abc\ntype: job-result\ncreated_by: agent\n---\nbody\n"
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

    # Simulate the PR being merged, then re-submit the identical note: the base now
    # contains it, so submit is an idempotent no-op (nothing to commit), not an error.
    for cmd in (
        ["checkout", "-q", "main"],
        ["merge", "-q", "note/job-abc"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(work), *cmd], check=True)
    ref_again = asyncio.run(submitter.submit(submission))
    assert ref_again == "note/job-abc"


def test_concurrent_submits_do_not_corrupt_branches(tmp_path: Path) -> None:
    """Two concurrent submits serialize: each remote branch holds exactly its own note.

    Without the submit lock, the interleaved `checkout -B` calls would land one
    note's file on the other note's branch (the checkout switches the whole tree).
    """
    remote, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    sub_a = _note_submission("job-a", content="note a\n")
    sub_b = _note_submission("job-b", content="note b\n")

    async def _both() -> tuple[str, str]:
        ref_a, ref_b = await asyncio.gather(submitter.submit(sub_a), submitter.submit(sub_b))
        return ref_a, ref_b

    assert asyncio.run(_both()) == ("note/job-a", "note/job-b")
    for branch, own, other in (
        ("note/job-a", "job-a.md", "job-b.md"),
        ("note/job-b", "job-b.md", "job-a.md"),
    ):
        files = subprocess.run(
            ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", branch],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert f"knowledge/job-result/{own}" in files
        assert other not in files


def test_repropose_updated_note_from_fresh_clone(tmp_path: Path) -> None:
    """Re-proposing an updated note from a clone that never fetched the branch works.

    `--force-with-lease` without a remote-tracking ref is a "stale info" rejection;
    the submitter must refresh the ref before pushing (tolerating a missing branch).
    """
    remote, work_a = _make_remote_and_clone(tmp_path)
    v1 = _note_submission("job-x", content="v1\n")
    submitter_a = GitNoteSubmitter(repo_dir=str(work_a), base_branch="main", remote="origin")
    asyncio.run(submitter_a.submit(v1))

    work_b = _clone(remote, tmp_path / "fresh")  # fresh clone: no origin/note/job-x ref
    v2 = v1.model_copy(update={"content": "v2\n"})
    submitter_b = GitNoteSubmitter(repo_dir=str(work_b), base_branch="main", remote="origin")
    assert asyncio.run(submitter_b.submit(v2)) == "note/job-x"

    shown = subprocess.run(
        ["git", "-C", str(remote), "show", "note/job-x:knowledge/job-result/job-x.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "v2\n"


def test_submitter_refuses_path_escaping_the_checkout(tmp_path: Path) -> None:
    """Defense in depth: a submission path resolving outside repo_dir is rejected."""
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    evil = NoteSubmission(
        branch="note/evil",
        path="../evil.md",
        content="x\n",
        title="evil",
        body="b",
    )
    with pytest.raises(GitSubmitError, match="escapes"):
        asyncio.run(submitter.submit(evil))
    assert not (tmp_path / "evil.md").exists()


def test_qm_workflow_publishes_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """With publish_to_graph, a completed QM job proposes a note on the bg queue."""
    fake = FakeSubmitter()
    monkeypatch.setattr(knowledge, "default_submitter", lambda: fake)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-hpc-pub",
                    workflows=[QMJobWorkflow],
                    activities=QM_ACTIVITIES,
                ),
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    activities=[write_knowledge_node],
                ),
            ):
                await client.execute_workflow(
                    QMJobWorkflow.run,
                    QMJobInput(
                        molecule_smiles="CCO",
                        method="B3LYP",
                        basis_set="def2-SVP",
                        publish_to_graph=True,
                    ),
                    id="qm-publish-test",
                    task_queue="test-hpc-pub",
                )
        assert len(fake.submissions) == 1  # the completed result was proposed as a note
        assert fake.submissions[0].path.startswith("knowledge/job-result/job-")

    asyncio.run(_run())
