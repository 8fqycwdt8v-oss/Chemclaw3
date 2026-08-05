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
from chemclaw.core.chem import compound_id
from chemclaw.core.config import settings
from chemclaw.durable.connector_job import ConnectorJobResult
from chemclaw.ingest.eln.compound import compound_dependencies
from chemclaw.kg.git_submitter import GitNoteSubmitter, GitSubmitError
from chemclaw.kg.note import Note
from chemclaw.kg.submission import NoteFile, NoteSubmission
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


def _current_branch(work: Path) -> str:
    """The branch `work` is checked out on right now."""
    return subprocess.run(
        ["git", "-C", str(work), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


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
    """After a submission, `note_repo_dir` is on `base` — because it was never taken off it.

    `note_repo_dir` is also where readers (`chemclaw.kg.graph.load_notes` et al.) resolve
    `settings.knowledge_path`, so a checkout on `note/<id>` makes every reader see one proposed
    note's isolated content instead of the merged knowledge base. This used to be a statement
    about *restoring* the tree; since D-2026-08-05 the submission happens in its own worktree and
    the tree is never switched, which `tests/test_pr_gate_read_window.py` states directly. Kept
    here as the end-to-end form of it, from the other side of the submitter.
    """
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(_note_submission("job-abc")))

    assert _current_branch(work) == "main"
    # The note this submission wrote is not on `main`'s working tree — only merging the PR
    # puts it there — so a reader pointed at this checkout right now sees no proposed notes.
    assert not (work / "knowledge" / "job-result" / "job-abc.md").exists()


def test_a_rejected_push_still_leaves_the_checkout_on_base(tmp_path: Path) -> None:
    """A submission that fails after the branch is created leaves nothing behind.

    Historically this was the PR-gate bypass a `try/finally` closed: a rejected push (a dead
    remote, a protected ref, a hook) left `note_repo_dir` on `note/<id>` with the unreviewed note
    in the working tree, served as merged knowledge by every reader and counted as merged by
    `ingest/eln/sync._merged_note_bodies`. The tree is no longer switched at all, so the failure
    path's obligation is a different one — dispose of the worktree — and that is asserted here
    too, because a `finally` that stops running is exactly how the original defect happened.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitSubmitError, match="push"):
        asyncio.run(submitter.submit(_note_submission("job-unreviewed")))

    assert _current_branch(work) == "main"
    assert not (work / "knowledge" / "job-result" / "job-unreviewed.md").exists()
    assert not list((work / ".git" / "chemclaw-worktrees").iterdir())


def test_a_failure_before_the_commit_leaves_no_note_in_the_tree(tmp_path: Path) -> None:
    """A note written but never staged reaches no reader either.

    A submission carries a note *and its dependencies*, so it can die between two `write_text`
    calls — here on the containment check of the second file. The first file is already on disk
    and untracked. It used to be discarded by the restoring `reset --hard` + `clean -fd`; it is
    now simply somewhere no reader looks, and goes with the worktree.
    """
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    pair = NoteSubmission(
        branch="note/job-pair",
        files=[
            NoteFile(path="knowledge/job-result/job-pair.md", content="the note\n"),
            NoteFile(path="../escape.md", content="the dependency\n"),
        ],
        title="Add job-result note: job-pair",
        body="review please",
    )

    with pytest.raises(GitSubmitError, match="escapes"):
        asyncio.run(submitter.submit(pair))

    assert _current_branch(work) == "main"
    assert not (work / "knowledge" / "job-result" / "job-pair.md").exists()
    assert not list((work / ".git" / "chemclaw-worktrees").iterdir())


def test_submit_leaves_a_readers_cache_alone_because_it_never_touches_their_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse of what this test asserted, and the clearest single statement of the fix.

    It used to assert that a submission cleared every cached graph, because a submission rewrote
    the shared working tree twice — into `note/<id>` and back — and a graph cached across that
    would describe a tree that no longer existed for up to `graph_cache_ttl_seconds`.

    The submission now happens in a worktree under `.git/` that no reader scans, so there is
    nothing to invalidate: busting would advertise a tree change that did not happen and pay an
    O(notes) rescan for it. "The cache survived" is only the right outcome if the cache is also
    still *correct*, so that is asserted too rather than assumed — the cached notes must equal what
    a cold scan of the same directory returns.
    """
    from chemclaw.kg import graph as kg_graph

    _, work = _make_remote_and_clone(tmp_path)
    notes_dir = work / "knowledge"
    notes_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    kg_graph.invalidate_cache()
    cached_before = [note.id for note in kg_graph.load_notes(notes_dir)]
    assert str(notes_dir) in kg_graph._LAST_SCAN

    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(_note_submission("job-xyz")))

    # The reader's window is untouched: no rescan was forced, because nothing it reads moved.
    assert str(notes_dir) in kg_graph._LAST_SCAN
    assert [note.id for note in kg_graph.load_notes(notes_dir)] == cached_before
    # And what it holds is what is really there — not-busting is correct, not merely observed.
    kg_graph.invalidate_cache()
    assert [note.id for note in kg_graph.load_notes(notes_dir)] == cached_before


def test_concurrent_submits_do_not_corrupt_branches(tmp_path: Path) -> None:
    """Two concurrent submits serialize: each remote branch holds exactly its own note.

    Without the submit lock the two would contend for `.git/worktrees/` and for the `note/<id>`
    refs — and, before the worktrees, for the one working tree, where the failure mode was not an
    error but one note's file committed onto the other note's branch.
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


def test_leading_dash_note_path_reaches_git_add_as_a_pathspec_not_an_option(
    tmp_path: Path,
) -> None:
    """A note path starting with `-` must be added as a file, never parsed as a git option (Sec-4).

    `_contained_note_path` only checks containment: `repo_root / "-u"` resolves *inside*
    `repo_root`, so this path passes it and reaches `git add` as a bare positional argument.
    Without `--` ending option parsing first, git reads `-u` as `--update` (stage only
    already-tracked changes, no pathspec) instead of the file it names — nothing new gets
    staged, `_write_and_push`'s "nothing to commit" idempotence check trips, and `submit`
    returns a branch name as if it had succeeded while the written note is never committed or
    pushed, then discarded unseen with the submission's worktree.
    """
    _, work = _make_remote_and_clone(tmp_path)
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    submission = NoteSubmission(
        branch="note/dash",
        files=[NoteFile(path="-u", content="body\n")],
        title="dash path",
        body="review please",
    )

    ref = asyncio.run(submitter.submit(submission))
    assert ref == "note/dash"

    remote_refs = subprocess.run(
        ["git", "-C", str(work), "ls-remote", "origin", "note/dash"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "note/dash" in remote_refs.stdout  # actually pushed, not silently dropped
    shown = subprocess.run(
        ["git", "-C", str(work), "show", "note/dash:-u"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "body\n"  # committed as a real file named "-u", not consumed as an option


def test_submit_refuses_the_checkout_the_process_runs_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitter pointed at the process's own checkout is refused before any git op.

    The reason changed and the guard did not. It used to protect uncommitted work from the
    `reset --hard` + `clean -fd` every submission ran; the submission no longer touches the shared
    tree, so that danger is gone. What remains is worse and still live: a submission creates
    `note/<id>` here and **force-pushes it to this repository's origin**, so pointed at the
    ChemClaw source checkout — which the `note_repo_dir="."` default resolves to — the gate would
    publish an agent-authored knowledge note into the code repository.

    Asserted as the absence of the mutation rather than as an exception alone: no note branch and
    no worktree may exist afterwards, which is what "refused before any git op" actually claims.
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

    # Nothing ran: no branch was created here, and no worktree. (The untracked file surviving is
    # no longer evidence of anything — the submitter could not destroy it even without the guard.)
    assert uncommitted.read_text(encoding="utf-8") == "do not destroy\n"
    branches = subprocess.run(
        ["git", "-C", str(work), "branch", "--list", "note/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip() == ""
    assert not (work / ".git" / "chemclaw-worktrees").exists()


def test_poisoned_index_does_not_leak_into_next_submission(tmp_path: Path) -> None:
    """Residue staged in the shared checkout is not committed into the next note's branch.

    A submission that died between `git add` and `git commit` used to leave its note staged in the
    shared index, and `checkout -B` preserves staged changes, so the next submission silently
    committed the stray into its own PR. A linked worktree has its own index, so the two cannot
    meet at all — the defence is structural rather than a scrub each time.

    Which means the stray is now *also* still staged afterwards, and that is asserted here: the
    mirror image of dropping `reset --hard`, and the clearest statement that the shared tree is no
    longer the submitter's to scrub.
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
    staged = subprocess.run(
        ["git", "-C", str(work), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "job-stray.md" in staged, "the operator's staged work is not the submitter's to discard"


def test_symlinked_directory_on_base_is_refused(tmp_path: Path) -> None:
    """A symlinked `knowledge` dir committed on the base branch cannot redirect the write.

    Containment must hold against the tree as it exists *after* the base branch is materialized:
    a symlink merged onto base would otherwise resolve as a real directory beforehand, pass the
    check, then be followed by the write. This is also the test that forbids creating the
    submission worktree with `--no-checkout` — with nothing on disk there is no symlink to
    resolve, the check passes vacuously, and this inverts.
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


def test_the_energy_line_carries_its_own_trust_and_not_a_bare_number() -> None:
    """F8-T1: a retrieval excerpt used to quote a confident figure with nothing attached.

    The bare `total energy: {x:.6f} Hartree` is what made this a defect — the number is
    indistinguishable from one the SCF never converged to, and the excerpt that quotes it back is
    a blind character prefix that cannot pick up a qualifier placed anywhere else.
    """
    body = note_from_qm_result(_RESULT).body
    line = next(ln for ln in body.splitlines() if ln.startswith("- total energy:"))
    # The unit is on the value line, and so is the statement about its uncertainty.
    assert "Hartree" in line
    assert "no uncertainty established" in line, (
        "the energy line states no uncertainty, which is the honest answer for an absolute "
        "energy — but it must say so rather than stay silent"
    )


def test_a_diverged_scf_is_flagged_on_the_number_itself() -> None:
    """Convergence is the QM domain question, and it has to reach the value a reader quotes.

    `- converged: False` on its own line is a fact a *human* can join up; a skill or a retrieval
    excerpt quoting the energy cannot. This is the mutation that matters: hard-code `in_domain`
    to True and a non-converged energy renders exactly like a converged one.
    """
    diverged = _RESULT.model_copy(update={"converged": False})
    line = next(
        ln
        for ln in note_from_qm_result(diverged).body.splitlines()
        if ln.startswith("- total energy:")
    )
    assert "OUT OF DOMAIN" in line
    assert "did not converge" in line

    converged = next(
        ln
        for ln in note_from_qm_result(_RESULT).body.splitlines()
        if ln.startswith("- total energy:")
    )
    assert "OUT OF DOMAIN" not in converged


def test_the_job_summary_and_the_note_agree_about_the_energy() -> None:
    """Two renderings of one number, and the summary is the line the chemist reads first.

    `_envelope`'s summary is what `get_durable_job_status` hands back on completion. It had its own
    hand-rolled `(converged)` / `(NOT converged)` parenthetical, so fixing only the note would have
    left the more-read surface saying less — and nothing in the suite rendered it at all, since the
    one test naming that string builds it as fixture data rather than calling `_envelope`.
    """
    from chemclaw.connectors.qm.workflows import _envelope

    for converged in (True, False):
        envelope = _envelope(_RESULT.model_copy(update={"converged": converged}), "")
        summary, note = envelope.summary, envelope.note
        assert summary is not None and note is not None
        energy_line = next(ln for ln in note.body.splitlines() if ln.startswith("- total energy:"))
        # The summary ends with exactly the rendering the note's energy line carries.
        assert summary.endswith(energy_line.removeprefix("- total energy: "))
        assert ("OUT OF DOMAIN" in summary) is (not converged)
