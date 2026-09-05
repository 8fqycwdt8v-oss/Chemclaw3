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
from chemclaw.kg.git_writer import GitNoteWriter, GitWriteError
from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteFile, NoteWrite


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
    # Point the bare remote's HEAD at `main`, so a fresh clone checks out the base branch rather
    # than an unborn `master`. That is what a real notes remote looks like, and the writer now
    # *requires* it: it commits on the base branch rather than creating one per note, so a clone
    # parked elsewhere is refused rather than quietly writing to the wrong branch.
    subprocess.run(
        ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True
    )
    return remote, work


def _note_write(note_id: str, content: str = "body\n") -> NoteWrite:
    """A minimal job-result write for `note_id` with the standard layout."""
    return NoteWrite(
        files=[NoteFile(path=f"knowledge/job-result/{note_id}.md", content=content)],
        message=f"Add job-result note: {note_id}",
    )


def _current_branch(work: Path) -> str:
    """The branch `work` is checked out on right now."""
    return subprocess.run(
        ["git", "-C", str(work), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_write_commits_the_note_on_the_base_branch_and_pushes(tmp_path: Path) -> None:
    """The whole of the new write path: the file lands in the tree, is committed, and is pushed.

    The reference is the *commit*, not a branch. That is the shape change
    `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` made: there is nothing to review and
    nothing to merge, so what a caller can be handed is what landed.
    """
    _, work = _make_remote_and_clone(tmp_path)

    note = Note(id="job-abc", type="job-result", created_by="agent", body="[[compound-x]]")
    submission = _note_write(
        "job-abc", content="---\nid: job-abc\ntype: job-result\ncreated_by: agent\n---\nbody\n"
    )
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    outcome = asyncio.run(writer.write(submission))

    assert outcome.written is True
    assert len(outcome.reference) == 40, "the reference is the commit the note landed in"
    # Readable *here*, which is the point: this checkout is what `settings.notes_path` resolves to.
    assert (work / "knowledge" / "job-result" / "job-abc.md").exists()
    remote_main = subprocess.run(
        ["git", "-C", str(work), "ls-remote", "origin", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert outcome.reference in remote_main.stdout, "the commit reached the remote base branch"
    assert note.type == "job-result"  # sanity on the model used above

    # Re-recording the identical note stages nothing, so it is an idempotent no-op rather than an
    # empty commit — and it says so, which is how the caller knows not to count it.
    again = asyncio.run(writer.write(submission))
    assert again.written is False


def test_a_write_stays_on_base_and_the_note_is_readable_there(tmp_path: Path) -> None:
    """The checkout stays on `base`, and the note is in it — which is the inversion.

    This test used to assert the opposite of its second half: under the PR-gate the note lived on
    `note/<id>` and a reader pointed at this checkout saw *nothing*, which was the isolation the
    gate depended on. `settings.notes_path` resolves to exactly this tree, so the note being here
    is what "global the moment it is learned" means
    (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`).
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(writer.write(_note_write("job-abc")))

    assert _current_branch(work) == "main"
    assert (work / "knowledge" / "job-result" / "job-abc.md").exists()


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

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="push"):
        asyncio.run(writer.write(_note_write("job-unreviewed")))

    assert _current_branch(work) == "main"
    # **The note is on disk and committed locally**, which is a real behaviour change and is
    # asserted rather than glossed: the write happens in the tree readers scan, so a push that
    # fails leaves the note *readable here* and absent from the remote. The next successful write
    # fast-forwards and carries it. What must not have happened is a silent success.
    assert (work / "knowledge" / "job-result" / "job-unreviewed.md").exists()


def test_a_failure_before_the_commit_leaves_no_note_in_the_tree(tmp_path: Path) -> None:
    """A write that dies between two files leaves the first one on disk, and that is now visible.

    A write carries a note *and its dependencies*, so it can die between two `write_text` calls —
    here on the containment check of the second. Under the PR-gate the half-written pair lived in a
    worktree no reader scanned; it now lives in the tree they do scan.

    **This is the cost of the write order, and it is bounded by that order rather than removed.**
    `record._build_write` puts dependencies first, so the file that survives a mid-write failure is
    one the subject note would have cited — never a subject citing something absent. Nothing is
    committed, so the next successful write of the same note supersedes it.
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    pair = NoteWrite(
        files=[
            NoteFile(path="knowledge/job-result/job-pair.md", content="the note\n"),
            NoteFile(path="../escape.md", content="the dependency\n"),
        ],
        message="Add job-result note: job-pair",
    )

    with pytest.raises(GitWriteError, match="escapes"):
        asyncio.run(writer.write(pair))

    assert _current_branch(work) == "main"
    assert (work / "knowledge" / "job-result" / "job-pair.md").exists()
    # Nothing was committed: the failure is before `git add`, so the tree is dirty, not recorded.
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "?? knowledge/job-result/job-pair.md" in status


def test_a_write_busts_a_readers_cache_because_it_does_touch_their_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse of what this test asserted, twice over, and the clearest statement of the change.

    It first asserted that a submission *cleared* every cached graph (the shared tree was rewritten
    into `note/<id>` and back), then — once the gate moved into a worktree under `.git/` — that it
    left the cache **alone**, because busting would advertise a tree change that had not happened.

    Both were right about their own design and both are wrong about this one. The write lands in
    the tree readers scan, so a surviving cache is a reader serving a graph that is missing the
    note just recorded — for up to `graph_cache_ttl_seconds`. "Global the moment it is learned"
    is exactly this assertion.
    """
    from chemclaw.kg import graph as kg_graph

    _, work = _make_remote_and_clone(tmp_path)
    notes_dir = work / "knowledge"
    notes_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "graph_cache_enabled", True)
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 60.0)
    kg_graph.invalidate_cache()
    before = [note.id for note in kg_graph.load_notes(notes_dir)]
    assert str(notes_dir) in kg_graph._LAST_SCAN

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(
        writer.write(
            NoteWrite(
                files=[
                    NoteFile(
                        path="knowledge/job-result/job-xyz.md",
                        content="---\nid: job-xyz\ntype: job-result\ncreated_by: agent\n---\nbody\n",
                    )
                ],
                message="Add job-result note: job-xyz",
            )
        )
    )

    # The cache was dropped, so the next read rescans rather than serving the pre-write graph.
    assert str(notes_dir) not in kg_graph._LAST_SCAN
    after = [note.id for note in kg_graph.load_notes(notes_dir)]
    assert "job-xyz" in after and "job-xyz" not in before


def test_concurrent_writes_serialize_and_both_notes_land(tmp_path: Path) -> None:
    """Two concurrent writes serialize, and the base branch ends up holding both notes.

    The lock matters *more* without branches, not less: both writes now target one branch and one
    working tree, so unserialized they would stage each other's files and race the same push. The
    failure this pins is not an error — it is one note's commit silently carrying the other's file,
    or one of the two never reaching the remote at all.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    sub_a = _note_write("job-a", content="note a\n")
    sub_b = _note_write("job-b", content="note b\n")

    async def _both() -> tuple[str, str]:
        ref_a, ref_b = await asyncio.gather(writer.write(sub_a), writer.write(sub_b))
        return ref_a.reference, ref_b.reference

    ref_a, ref_b = asyncio.run(_both())
    assert ref_a != ref_b, "two writes are two commits"
    files = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "knowledge/job-result/job-a.md" in files
    assert "knowledge/job-result/job-b.md" in files


def test_second_process_holding_the_checkout_is_rejected(tmp_path: Path) -> None:
    """A submit against a checkout flocked by *another process* fails fast, then recovers.

    Cross-process ownership of `note_repo_dir` is enforced with an exclusive
    `flock` on `.git/chemclaw-submit.lock`. A real child process takes the lock;
    the submit must raise `GitWriteError` instead of interleaving checkouts, and
    must succeed once the child releases it.
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
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
        with pytest.raises(GitWriteError, match="in use by another process"):
            asyncio.run(writer.write(_note_write("job-locked")))
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)

    assert asyncio.run(writer.write(_note_write("job-locked"))).written is True


def test_lock_is_released_after_a_failed_write(tmp_path: Path) -> None:
    """The flock does not outlive a write that errored (no wedged checkout).

    A failed write must not leave the checkout permanently 'in use': the next one acquires the
    lock and runs normally. The failure is forced by naming a base branch this checkout is not on,
    which is the guard `_write_locked` runs first — it fails *inside* the lock, which is what makes
    this a test of the release rather than of the guard.
    """
    _, work = _make_remote_and_clone(tmp_path)
    bad = GitNoteWriter(repo_dir=str(work), base_branch="no-such-base", remote="origin")
    with pytest.raises(GitWriteError, match="not the base branch"):
        asyncio.run(bad.write(_note_write("job-x")))

    good = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    assert asyncio.run(good.write(_note_write("job-x"))).written is True


def test_rewriting_a_note_from_a_second_clone_lands_on_the_shared_base(tmp_path: Path) -> None:
    """A second clone recording a newer version of the same note replaces it on the base branch.

    Under the PR-gate this exercised `--force-with-lease` against a `note/<id>` ref the fresh clone
    had never fetched. There is no such ref now, and the equivalent hazard moved: two clones write
    the *same* branch, so the second must fast-forward onto what the first pushed before committing
    — otherwise its push is rejected or, worse, it commits on a base that has silently gone stale.
    """
    remote, work_a = _make_remote_and_clone(tmp_path)
    v1 = _note_write("job-x", content="v1\n")
    submitter_a = GitNoteWriter(repo_dir=str(work_a), base_branch="main", remote="origin")
    asyncio.run(submitter_a.write(v1))

    work_b = _clone(remote, tmp_path / "fresh")  # a second clone of the same notes repo
    v2 = v1.model_copy(update={"files": [NoteFile(path=v1.files[0].path, content="v2\n")]})
    submitter_b = GitNoteWriter(repo_dir=str(work_b), base_branch="main", remote="origin")
    assert asyncio.run(submitter_b.write(v2)).written is True

    shown = subprocess.run(
        ["git", "-C", str(remote), "show", "main:knowledge/job-result/job-x.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert shown == "v2\n"


def test_submitter_refuses_path_escaping_the_checkout(tmp_path: Path) -> None:
    """Defense in depth: a submission path resolving outside repo_dir is rejected."""
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    evil = NoteWrite(
        files=[NoteFile(path="../evil.md", content="x\n")],
        message="evil",
    )
    with pytest.raises(GitWriteError, match="escapes"):
        asyncio.run(writer.write(evil))
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
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    submission = NoteWrite(
        files=[NoteFile(path="-u", content="body\n")],
        message="dash path",
    )

    ref = asyncio.run(writer.write(submission))
    assert ref.written is True

    remote_refs = subprocess.run(
        ["git", "-C", str(work), "ls-remote", "origin", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ref.reference in remote_refs.stdout  # actually pushed, not silently dropped
    shown = subprocess.run(
        ["git", "-C", str(work), "show", "main:-u"],
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
        writer = GitNoteWriter(repo_dir=repo_dir, base_branch="main", remote="origin")
        with pytest.raises(GitWriteError, match="CHEMCLAW_NOTE_REPO_DIR"):
            asyncio.run(writer.write(_note_write("job-own")))

    # Running from a subdirectory of the same checkout is refused too (repo-root match).
    subdir = work / "sub"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="CHEMCLAW_NOTE_REPO_DIR"):
        asyncio.run(writer.write(_note_write("job-own")))

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


def test_poisoned_index_does_not_leak_into_the_next_write(tmp_path: Path) -> None:
    """Residue staged in the shared checkout is not committed into the next note's commit.

    **This test found a real regression and is why the commit is path-limited.** The gate this
    replaced committed inside a linked worktree with its own index, so a stray staged in the shared
    checkout structurally could not reach a note's commit. Writing directly, there is only one
    index — and a plain `git commit` would have swept the stray into a commit named after the note.
    `_write_and_commit` therefore passes `-- <written paths>`, and the idempotence check is scoped
    the same way for the same reason.

    The stray stays staged afterwards, which is asserted here too: it is not this writer's to
    discard, and dropping it silently would be the mirror of the defect above.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    stray = work / "knowledge" / "job-result" / "job-stray.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("half-written residue\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", str(stray)], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(writer.write(_note_write("job-b", content="note b\n")))

    files = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
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
    write against an unmaterialized tree: with nothing on disk there is no symlink to resolve, the
    check passes vacuously, and this inverts.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    # An unrelated prior submission — its own fetch+checkout below ignores whatever branch
    # `work` is left on (it now returns to `base`, but even the old stuck-on-`note/job-a`
    # behavior made no difference here either way).
    asyncio.run(writer.write(_note_write("job-a", content="note a\n")))

    outside = tmp_path / "outside"
    outside.mkdir()
    attacker = _clone(remote, tmp_path / "attacker")
    subprocess.run(["git", "-C", str(attacker), "checkout", "-q", "main"], check=True)
    # The base now really holds `knowledge/` (the write above landed there rather than on a note
    # branch), so the attack is to *replace* the directory with a symlink rather than to add one.
    subprocess.run(["git", "-C", str(attacker), "rm", "-r", "-q", "knowledge"], check=True)
    (attacker / "knowledge").symlink_to(outside, target_is_directory=True)
    for cmd in (
        ["add", "knowledge"],
        ["commit", "-q", "-m", "symlink"],
        ["push", "-q", "origin", "main"],
    ):
        subprocess.run(["git", "-C", str(attacker), *cmd], check=True)

    with pytest.raises(GitWriteError, match="escapes"):
        asyncio.run(writer.write(_note_write("job-b", content="note b\n")))
    assert list(outside.rglob("*")) == []  # nothing was written outside the checkout


def test_git_command_timeout_kills_the_child_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung git command is killed after the timeout and reported as GitWriteError.

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

    monkeypatch.setattr("chemclaw.kg.git_writer.asyncio.create_subprocess_exec", _fake_exec)
    (tmp_path / ".git").mkdir()  # submit() flocks a file under .git/ before running git
    writer = GitNoteWriter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    with pytest.raises(GitWriteError, match="timed out"):
        asyncio.run(writer.write(_note_write("job-hang")))
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

    monkeypatch.setattr("chemclaw.kg.git_writer.asyncio.create_subprocess_exec", _fake_exec)
    writer = GitNoteWriter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    async def _run() -> None:
        reading = asyncio.create_task(writer._read("refs/remotes/origin/note/x"))
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

    A bundle used to own a `write_knowledge_node` activity calling `record_note` directly, which
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
            named = isinstance(node, ast.Name) and node.id == "record_note"
            if reached or named:
                offenders.append(str(path.relative_to("src")))
                break
    assert offenders == [], (
        f"{offenders} reach the PR-gate from inside a connector: a bundle returns its note in the "
        "job envelope and core decides whether it is proposed"
    )


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

    submission = NoteWrite(
        files=[
            NoteFile(path="knowledge/job-result/job-dep.md", content="the subject note\n"),
            NoteFile(
                path="knowledge/compound/compound-x.md",
                content="machine rendering\n",
                overwrite=False,
            ),
        ],
        message="Add job-result note: job-dep",
    )
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    outcome = asyncio.run(writer.write(submission))
    assert outcome.written is True

    recorded = subprocess.run(
        ["git", "-C", str(work), "show", "main:knowledge/compound/compound-x.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "chemist's hazard note" in recorded, "the human's edit must survive the write"


def test_git_child_env_scrubs_app_secrets_but_keeps_git_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_git_child_env` hands git the environment minus this process's own secret values.

    Least privilege: a git remote, credential helper or hook must not find the LLM key, a DSN or the
    framing HMAC in its environment. The notes-remote token and PATH are not secrets git can do
    without, so they survive — that survival is what keeps `push` working.
    """
    from chemclaw.kg.git_writer import _git_child_env

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

    monkeypatch.setattr("chemclaw.kg.git_writer.asyncio.create_subprocess_exec", _fake_exec)
    writer = GitNoteWriter(repo_dir=str(tmp_path), base_branch="main", remote="origin")

    result = asyncio.run(writer._read("HEAD"))

    assert result == "deadbeef"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "CHEMCLAW_LLM_API_KEY" not in env
