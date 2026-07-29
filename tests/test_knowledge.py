"""Tests for the result→note bridge and the git submitter (plan step 2.8).

The bridge is a *mapping* now, not an activity: the `qm` bundle builds the note and core publishes
it through the PR-gate from the job envelope (D-118), which is why nothing here submits a note on
the QM job's behalf any more — `tests/test_connector_job_workflow.py` owns that half.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.connectors.qm.knowledge import note_from_qm_result
from chemclaw.connectors.qm.specs import QMJobResult, QmJobSpec
from chemclaw.connectors.qm.workflows import QMJobWorkflow
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobResult
from chemclaw.ingest.eln.compound import compound_dependencies, compound_id
from chemclaw.kg.git_submitter import GitNoteSubmitter, GitSubmitError
from chemclaw.kg.note import Note
from chemclaw.kg.pr_gate import NoteFile, NoteSubmission
from tests.temporal_env import QM_ACTIVITIES, pydantic_client, start_env_or_skip

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


def test_a_job_result_links_its_compound_and_brings_it_along() -> None:
    """The crosslink, and the reason it is now safe to make (STO-7).

    This assertion used to read `note.outgoing_links() == []`, with a comment explaining that a
    wikilink to a possibly-absent compound note would dangle and fail `kg-validate` on the very PR
    that added it. That was true, and it made every computed result a graph island — the
    calculation store and the note graph could not reference each other in either direction.

    What changed is the PR-gate: a submission carries a note *with its dependencies*, so the link
    and its target land together and the link resolves on the branch it is proposed on.
    """
    note = note_from_qm_result(_RESULT)
    expected = compound_id("CCO")
    assert note.outgoing_links() == [expected]

    # ...and the note it links is minted into the same submission, so the link is not dangling.
    dependencies = compound_dependencies(note)
    assert [dependency.id for dependency in dependencies] == [expected]
    assert dependencies[0].type == "compound"


def test_the_bundle_has_no_way_to_write_the_note_itself() -> None:
    """The QM bundle *builds* a note and cannot *publish* one — the GxP asymmetry, structurally.

    It used to own a `write_knowledge_node` activity that called `propose_note` directly, which
    made "AI proposes, human signs off" a convention the bundle chose to honour rather than a
    boundary it could not cross. Core publishes whatever note the job envelope carries now, so a
    connector reaching the graph would first have to import the PR-gate — and no bundle does.
    """
    import chemclaw.connectors.qm.knowledge as qm_knowledge

    assert not hasattr(qm_knowledge, "write_knowledge_node")
    source = Path(qm_knowledge.__file__).read_text(encoding="utf-8")
    assert "pr_gate" not in source and "propose_note" not in source


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
        files=[NoteFile(path=f"knowledge/job-result/{note_id}.md", content=content)],
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


def test_submit_leaves_the_shared_checkout_on_base(tmp_path: Path) -> None:
    """After a submission, `note_repo_dir` is back on `base` — not stuck on the note branch.

    `note_repo_dir` is also where readers (`chemclaw.kg.graph.load_notes` et al.) resolve
    `settings.knowledge_path`, so a checkout left on `note/<id>` would make every reader
    see one proposed note's isolated content instead of the merged knowledge base until
    the next submission happened to switch branches again first (the bug this proves fixed).
    """
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(_note_submission("job-abc")))

    current_branch = subprocess.run(
        ["git", "-C", str(work), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current_branch == "main"
    # The note this submission wrote is not on `main`'s working tree — only merging the PR
    # puts it there — so a reader pointed at this checkout right now sees no proposed notes.
    assert not (work / "knowledge" / "job-result" / "job-abc.md").exists()


def test_submit_busts_the_graph_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The authoring loop never waits out the TTL window: submitting a note invalidates the cache.

    Without this the `graph_cache_ttl_seconds` window (DA-5) could serve a note the process just
    wrote as absent — and the submitter's `checkout -B`/`reset --hard` rewrite the tree wholesale,
    so a stale cached graph could describe a tree that no longer exists.
    """
    from chemclaw.kg import graph as kg_graph

    _, work = _make_remote_and_clone(tmp_path)
    notes_dir = work / "knowledge"
    notes_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    kg_graph.invalidate_cache()
    kg_graph.load_notes(notes_dir)  # populate the cache and open the TTL window
    assert str(notes_dir) in kg_graph._LAST_SCAN

    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(_note_submission("job-xyz")))

    assert kg_graph._LAST_SCAN == {}  # the window was closed, so the next read re-scans


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


def test_second_process_holding_the_checkout_is_rejected(tmp_path: Path) -> None:
    """A submit against a checkout flocked by *another process* fails fast, then recovers.

    Cross-process ownership of `note_repo_dir` is enforced with an exclusive
    `flock` on `.git/chemclaw-submit.lock`. A real child process takes the lock;
    the submit must raise `GitSubmitError` instead of interleaving checkouts, and
    must succeed once the child releases it.
    """
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    lock_path = work / ".git" / "chemclaw-submit.lock"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            f"f = open({str(lock_path)!r}, 'a')\n"
            "fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('locked', flush=True)\n"
            "sys.stdin.readline()\n",  # hold the lock until the parent closes stdin
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(GitSubmitError, match="in use by another process"):
            asyncio.run(submitter.submit(_note_submission("job-locked")))
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)

    assert asyncio.run(submitter.submit(_note_submission("job-locked"))) == "note/job-locked"


def test_lock_is_released_after_a_failed_submission(tmp_path: Path) -> None:
    """The flock does not outlive a submission that errored (no wedged checkout).

    A failed git command must not leave the checkout permanently 'in use': the
    next submit acquires the lock and runs normally.
    """
    _, work = _make_remote_and_clone(tmp_path)
    bad = GitNoteSubmitter(repo_dir=str(work), base_branch="no-such-base", remote="origin")
    with pytest.raises(GitSubmitError, match="fetch"):
        asyncio.run(bad.submit(_note_submission("job-x")))

    good = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    assert asyncio.run(good.submit(_note_submission("job-x"))) == "note/job-x"


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
    v2 = v1.model_copy(update={"files": [NoteFile(path=v1.files[0].path, content="v2\n")]})
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
        files=[NoteFile(path="../evil.md", content="x\n")],
        title="evil",
        body="b",
    )
    with pytest.raises(GitSubmitError, match="escapes"):
        asyncio.run(submitter.submit(evil))
    assert not (tmp_path / "evil.md").exists()


def test_submit_refuses_the_checkout_the_process_runs_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitter pointed at the process's own checkout is refused before any git op.

    Every submission starts with `git reset --hard` + `git clean -fd`; against a
    non-dedicated checkout (the `note_repo_dir="."` default resolves to the process
    CWD — typically the developer's own repo) that would silently destroy uncommitted
    work and untracked files. The refusal must fire before anything destructive runs.
    """
    _, work = _make_remote_and_clone(tmp_path)
    uncommitted = work / "work-in-progress.txt"
    uncommitted.write_text("do not destroy\n", encoding="utf-8")

    monkeypatch.chdir(work)
    for repo_dir in (".", str(work)):
        submitter = GitNoteSubmitter(repo_dir=repo_dir, base_branch="main", remote="origin")
        with pytest.raises(GitSubmitError, match="CHEMCLAW_NOTE_REPO_DIR"):
            asyncio.run(submitter.submit(_note_submission("job-own")))

    # Running from a subdirectory of the same checkout is refused too (repo-root match).
    subdir = work / "sub"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitSubmitError, match="CHEMCLAW_NOTE_REPO_DIR"):
        asyncio.run(submitter.submit(_note_submission("job-own")))

    assert uncommitted.read_text(encoding="utf-8") == "do not destroy\n"  # nothing was wiped


def test_poisoned_index_does_not_leak_into_next_submission(tmp_path: Path) -> None:
    """Residue staged by a failed prior submission is not committed into the next note's branch.

    A submission that dies between `git add` and `git commit` (timeout kill, rejecting
    hook) leaves its note staged; `checkout -B` preserves staged changes, so without a
    reset the next submission would silently commit the stray note into its own PR.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    stray = work / "knowledge" / "job-result" / "job-stray.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("half-written residue\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", str(stray)], check=True)

    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(_note_submission("job-b", content="note b\n")))

    files = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "note/job-b"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "knowledge/job-result/job-b.md" in files
    assert "job-stray.md" not in files


def test_symlinked_directory_on_base_is_refused(tmp_path: Path) -> None:
    """A symlinked `knowledge` dir committed on the base branch cannot redirect the write.

    Containment must hold against the tree as it exists *after* `checkout -B` swaps
    in the base branch: a symlink merged onto base would otherwise resolve as a real
    directory pre-checkout, pass the check, then be followed by the write.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    # An unrelated prior submission — its own fetch+checkout below ignores whatever branch
    # `work` is left on (it now returns to `base`, but even the old stuck-on-`note/job-a`
    # behavior made no difference here either way).
    asyncio.run(submitter.submit(_note_submission("job-a", content="note a\n")))

    outside = tmp_path / "outside"
    outside.mkdir()
    attacker = _clone(remote, tmp_path / "attacker")
    subprocess.run(["git", "-C", str(attacker), "checkout", "-q", "main"], check=True)
    (attacker / "knowledge").symlink_to(outside, target_is_directory=True)
    for cmd in (
        ["add", "knowledge"],
        ["commit", "-q", "-m", "symlink"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(attacker), *cmd], check=True)

    with pytest.raises(GitSubmitError, match="escapes"):
        asyncio.run(submitter.submit(_note_submission("job-b", content="note b\n")))
    assert list(outside.rglob("*")) == []  # nothing was written outside the checkout


def test_git_command_timeout_kills_the_child_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung git command is killed after the timeout and reported as GitSubmitError.

    Without the bound, `communicate()` would await forever under the process-wide submit
    lock, deadlocking every other submission and orphaning the git child.
    """
    monkeypatch.setattr(settings, "git_command_timeout_seconds", 0.05)
    killed = {"value": False}

    class _HangingProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)  # never returns within the timeout
            return b"", b""

        def kill(self) -> None:
            killed["value"] = True

        async def wait(self) -> int:
            return -9

    async def _fake_exec(*_args: object, **_kwargs: object) -> _HangingProcess:
        return _HangingProcess()

    monkeypatch.setattr("chemclaw.kg.git_submitter.asyncio.create_subprocess_exec", _fake_exec)
    (tmp_path / ".git").mkdir()  # submit() flocks a file under .git/ before running git
    submitter = GitNoteSubmitter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    with pytest.raises(GitSubmitError, match="timed out"):
        asyncio.run(submitter.submit(_note_submission("job-hang")))
    assert killed["value"] is True


def test_qm_workflow_hands_its_note_to_core_in_the_envelope() -> None:
    """A completed QM run returns the note for core to PR-gate, rather than writing it.

    The bundle's half of the publish contract, proven on a real server: whether that note reaches
    the graph is `publish_to_graph` in `connectors/qm/connector.yaml`, and the publishing itself is
    `ConnectorJobWorkflow`'s (covered by `tests/test_connector_job_workflow.py`).
    """

    async def _run() -> ConnectorJobResult:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-qm-pub",
                workflows=[QMJobWorkflow],
                activities=QM_ACTIVITIES,
            ):
                result: ConnectorJobResult = await client.execute_workflow(
                    QMJobWorkflow.run,
                    QmJobSpec(molecule_smiles="CCO", method="B3LYP", basis_set="def2-SVP"),
                    id="qm-publish-test",
                    task_queue="test-qm-pub",
                )
                return result

    result = asyncio.run(_run())
    assert result.note is not None
    assert result.note.type == "job-result"
    assert result.note.created_by == "agent"  # so the PR-gate is the only way in
