"""Tests for the git submitter behind the PR-gate (plan step 2.8), and the boundary it enforces.

A bundle *builds* a note and cannot *publish* one: core publishes whatever note the job envelope
carries (D-118), which is why nothing here submits a note on a connector's behalf —
`tests/test_connector_job_workflow.py` owns that half, and the last test in this file asserts that
no bundle has a second way in.
"""

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.kg.git_submitter import GitNoteSubmitter, GitSubmitError
from chemclaw.kg.note import Note
from chemclaw.kg.submission import NoteFile, NoteSubmission


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
    outcome = asyncio.run(submitter.submit(submission))

    assert outcome.reference == "note/job-abc" and outcome.pushed is True
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
    again = asyncio.run(submitter.submit(submission))
    # The idempotent no-op now *says* it pushed nothing, so the caller can skip the record.
    assert again.reference == "note/job-abc" and again.pushed is False


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
    in the working tree, served as merged knowledge by every reader and counted as merged by the
    ELN sync's corpus scan (since deleted with the ELN half of the gate, D-2026-08-25). The tree is
    no longer switched at all, so the failure
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
        return ref_a.reference, ref_b.reference

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

    assert (
        asyncio.run(submitter.submit(_note_submission("job-locked"))).reference == "note/job-locked"
    )


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
    assert asyncio.run(good.submit(_note_submission("job-x"))).reference == "note/job-x"


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
    assert asyncio.run(submitter_b.submit(v2)).reference == "note/job-x"

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
    assert ref.reference == "note/dash"

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


def test_a_cancelled_git_read_kills_its_child_like_every_other_git_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation must not orphan a git process, whichever helper issued the command.

    `_run`'s docstring says "**Every** git command goes through here ... the timeout and the
    kill-on-cancel are properties of this function, and a command issued any other way would be
    unbounded and invisible at once". `_read` issued its own `create_subprocess_exec` and is
    called three times per submission from `_require_gate_authored_tip`. Half of that claim was
    already false in the reassuring direction — `_read` did carry `git_command_timeout_seconds` —
    but it had no `except asyncio.CancelledError` arm, so a submission cancelled mid-read (a
    Temporal activity timeout is the live case) left the `git rev-parse`/`git log` child running.

    Driven on `_read` directly rather than through `submit`, because the property is the helper's
    and a submission would reach it only after a real checkout.
    """
    killed = {"value": False}

    class _HangingProcess:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(30)  # cancelled here, never returns
            return b"", b""

        def kill(self) -> None:
            killed["value"] = True

        async def wait(self) -> int:
            return -9

    async def _fake_exec(*_args: object, **_kwargs: object) -> _HangingProcess:
        return _HangingProcess()

    monkeypatch.setattr("chemclaw.kg.git_submitter.asyncio.create_subprocess_exec", _fake_exec)
    submitter = GitNoteSubmitter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    async def _run() -> None:
        reading = asyncio.create_task(submitter._read("refs/remotes/origin/note/x"))
        await asyncio.sleep(0.05)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading

    asyncio.run(_run())

    assert killed["value"] is True, (
        "a cancelled `_read` left its git child running — the orphan `_run` has an arm for"
    )


def test_no_connector_bundle_can_reach_the_pr_gate_itself() -> None:
    """The review asymmetry, structurally rather than by convention.

    A bundle used to own a `write_knowledge_node` activity calling `propose_note` directly, which
    made "the agent proposes, a human decides" something the bundle chose to honour rather than a
    boundary it could not cross. Core publishes the envelope's note now, so a connector reaching
    the graph would first have to import the PR-gate.

    Asserted over **every** bundle rather than against one module's attribute, which is what it
    used to be: that spelling named the `qm` bundle, so it went dark the day that bundle was
    removed (`D-2026-08-26-semiempirical-is-the-whole-tier`) and would have protected nothing
    while still reading as a control. `chemclaw.connectors -> chemclaw.kg` is an allowed edge in
    `tests/test_layering.py` — bundles legitimately build `Note` objects — so this is the rule that
    narrows it to *building*.
    """
    bundles = Path("src/chemclaw/connectors")
    offenders = []
    for path in sorted(bundles.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Imports and calls only — a *docstring* naming the gate is the point being made, not a
        # violation of it, and `connectors/manifest.py` makes exactly that point.
        for node in ast.walk(tree):
            reached = (
                isinstance(node, ast.ImportFrom) and (node.module or "").endswith("kg.pr_gate")
            ) or (
                isinstance(node, ast.Import)
                and any(alias.name.endswith("kg.pr_gate") for alias in node.names)
            )
            named = isinstance(node, ast.Name) and node.id == "propose_note"
            if reached or named:
                offenders.append(str(path.relative_to("src")))
                break
    assert offenders == [], (
        f"{offenders} reach the PR-gate from inside a connector: a bundle returns its note in the "
        "job envelope and core decides whether it is proposed"
    )


def test_a_branch_a_human_pushed_to_is_never_replaced(tmp_path: Path) -> None:
    """The property `--force-with-lease` could not carry: a reviewer's commit survives.

    The lease is refreshed by the fetch every submission starts with, so it only guards the
    fetch-to-push window — the reviewer's commit that was already on the branch matched the fresh
    lease and was silently discarded. The tip guard reads the gate's own commit trailer instead:
    a tip the gate did not mint refuses the submission, non-retryably, with instructions.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    v1 = _note_submission("job-x", content="v1\n")
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(submitter.submit(v1))

    # A reviewer clones, commits a fixup onto the proposal branch, and pushes it.
    reviewer = _clone(remote, tmp_path / "reviewer")
    subprocess.run(["git", "-C", str(reviewer), "checkout", "-q", "note/job-x"], check=True)
    fixup = reviewer / "knowledge" / "job-result" / "job-x.md"
    fixup.write_text("v1, with the reviewer's correction\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(reviewer), "commit", "-aqm", "reviewer fixup"], check=True)
    subprocess.run(["git", "-C", str(reviewer), "push", "-q", "origin", "note/job-x"], check=True)

    v2 = v1.model_copy(update={"files": [NoteFile(path=v1.files[0].path, content="v2\n")]})
    with pytest.raises(GitSubmitError, match="did not author"):
        asyncio.run(submitter.submit(v2))

    # And the reviewer's commit is still the remote tip.
    tip = subprocess.run(
        ["git", "-C", str(reviewer), "ls-remote", "origin", "note/job-x"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    local = subprocess.run(
        ["git", "-C", str(reviewer), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert tip == local


def test_a_dependency_never_overwrites_a_human_edited_file(tmp_path: Path) -> None:
    """`overwrite=False` files are written only where the base branch has none.

    A machine-rendered compound note re-rides on every proposal that links it; written
    unconditionally, it silently reverted a chemist's post-merge edit inside a PR titled as an
    addition.
    """
    _, work = _make_remote_and_clone(tmp_path)
    # The base branch already carries the dependency, edited by a human after merge.
    edited = work / "knowledge" / "compound" / "compound-x.md"
    edited.parent.mkdir(parents=True)
    edited.write_text("machine rendering, plus a chemist's hazard note\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "human edit"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)

    submission = NoteSubmission(
        branch="note/job-dep",
        files=[
            NoteFile(path="knowledge/job-result/job-dep.md", content="the subject note\n"),
            NoteFile(
                path="knowledge/compound/compound-x.md",
                content="machine rendering\n",
                overwrite=False,
            ),
        ],
        title="Add job-result note: job-dep",
        body="review please",
    )
    submitter = GitNoteSubmitter(repo_dir=str(work), base_branch="main", remote="origin")
    outcome = asyncio.run(submitter.submit(submission))
    assert outcome.pushed is True

    on_branch = subprocess.run(
        ["git", "-C", str(work), "show", "note/job-dep:knowledge/compound/compound-x.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "chemist's hazard note" in on_branch, "the human's edit must survive the proposal"


def test_git_child_env_scrubs_app_secrets_but_keeps_git_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_git_child_env` hands git the environment minus this process's own secret values.

    Least privilege: a git remote, credential helper or hook must not find the LLM key, a DSN or the
    framing HMAC in its environment. The notes-remote token and PATH are not secrets git can do
    without, so they survive — that survival is what keeps `push` working.
    """
    from chemclaw.kg.git_submitter import _git_child_env

    monkeypatch.setenv("CHEMCLAW_LLM_API_KEY", "llm-secret-value")
    monkeypatch.setenv("CHEMCLAW_POSTGRES_DSN", "postgresql://u:pw@db/x")
    monkeypatch.setenv("CHEMCLAW_FRAMING_ENVELOPE_SECRET", "hmac-secret-value")
    monkeypatch.setenv("CHEMCLAW_KNOWLEDGE_REPO_TOKEN", "git-token-value")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = _git_child_env()

    assert "CHEMCLAW_LLM_API_KEY" not in env
    assert "CHEMCLAW_POSTGRES_DSN" not in env
    assert "CHEMCLAW_FRAMING_ENVELOPE_SECRET" not in env
    assert env["CHEMCLAW_KNOWLEDGE_REPO_TOKEN"] == "git-token-value"
    assert env["PATH"] == "/usr/bin:/bin"


def test_git_subprocess_receives_the_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scrubbed environment actually reaches `create_subprocess_exec`, not just the helper."""
    monkeypatch.setenv("CHEMCLAW_LLM_API_KEY", "llm-secret-value")
    captured: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"deadbeef\n", b""

        def kill(self) -> None:  # pragma: no cover - not reached on a clean exit
            pass

        async def wait(self) -> int:  # pragma: no cover
            return 0

    async def _fake_exec(*_args: object, **kwargs: object) -> _FakeProcess:
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr("chemclaw.kg.git_submitter.asyncio.create_subprocess_exec", _fake_exec)
    submitter = GitNoteSubmitter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    result = asyncio.run(submitter._read("HEAD"))

    assert result == "deadbeef"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "CHEMCLAW_LLM_API_KEY" not in env
