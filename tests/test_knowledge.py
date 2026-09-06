"""Tests for `GitNoteWriter` — the one path a note takes from this system into the graph.

A bundle *builds* a note and cannot *write* one: core writes whatever note the job envelope
carries (D-118), which is why nothing here writes a note on a connector's behalf —
`tests/test_connector_job_workflow.py` owns that half, and the last test in this file asserts that
no bundle has a second way in.
"""

import ast
import asyncio
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.kg.git_writer import (
    GitNoteWriter,
    GitRemoteError,
    GitWriteError,
    _replace_atomically,
)
from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteFile, NoteWrite, record_note


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
    """A push that fails leaves the checkout on its base branch, with the note committed locally.

    Historically this was the PR-gate bypass a `try/finally` closed: a rejected push (a dead
    remote, a protected ref, a hook) left `note_repo_dir` on `note/<id>` with the unreviewed note
    in the working tree, served as merged knowledge by every reader and counted as merged by the
    ELN sync's corpus scan (since deleted with the ELN half of the gate, D-2026-08-25).

    The tree is no longer switched at all, so what is left to assert is what the failure *does*
    leave: the base branch, and a local commit the next successful write carries. Nothing here
    claims a worktree is disposed of — there is no worktree — and an earlier version of this
    docstring said so while the body asserted nothing of the kind.
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
    """A write that dies on any file leaves none of them in the tree readers scan.

    A write carries a note *and its dependencies*, so it can die part-way — here on the containment
    check of the second file. Under the PR-gate the half-written pair lived in a worktree no reader
    scanned, so this cost nothing; writing into the tree readers *do* scan, a surviving first file
    is a published half-unit.

    **This test's name asserted that and its body asserted the opposite**, because the first version
    of the direct writer validated each path as it wrote. Paths are now resolved and checked in a
    pass of their own before any byte lands, and anything already written is restored if a later
    step raises — so the name is true again.
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
    assert not (work / "knowledge" / "job-result" / "job-pair.md").exists()
    # And the tree is clean, not merely uncommitted: a restored write leaves no untracked residue.
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""


def _refuse_every_commit(work: Path) -> Path:
    """Install a `pre-commit` hook that fails, and return it — a site policy hook, as deployed."""
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'site policy hook says no' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_a_failed_commit_leaves_nothing_staged_and_the_pod_can_write_again(
    tmp_path: Path,
) -> None:
    """A commit that fails after `git add` un-stages what it staged — or it wedges the pod.

    The failure is real rather than injected: a `pre-commit` hook that exits non-zero, which is
    also what an `index.lock` or `_exec`'s timeout kill looks like from here. The rollback used to
    restore only the *working tree*, so the retracted blob stayed in the index. Two things follow,
    and both are asserted: the un-published content sits staged in the clone an operator and the
    knowledge-sync sidecar share, and the next `merge --ff-only` refuses because of it — after
    which `_replay_our_unpushed_commits` finds no commits of ours to replay and refuses in turn,
    so **every** later write on this pod fails forever.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    note = work / "knowledge" / "job-result" / "job-x.md"
    note.parent.mkdir(parents=True)
    note.write_text("original body\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "seed note"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)

    hook = _refuse_every_commit(work)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="site policy hook"):
        asyncio.run(writer.write(_note_write("job-x", content="NEW BODY\n")))

    assert note.read_text(encoding="utf-8") == "original body\n", "the tree is restored"
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == "", f"the index still holds the retracted write: {status!r}"

    # The hook is fixed (or the lock cleared), and meanwhile another pod moved the same file.
    hook.unlink()
    other = _clone(remote, tmp_path / "other")
    (other / "knowledge" / "job-result" / "job-x.md").write_text(
        "from another pod\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(other), "commit", "-q", "-am", "other pod"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)

    outcome = asyncio.run(writer.write(_note_write("job-y", content="Y\n")))
    assert outcome.written is True
    assert (work / "knowledge" / "job-result" / "job-y.md").exists()


def test_a_checkout_with_no_local_commits_is_not_reported_as_unauthored_ones(
    tmp_path: Path,
) -> None:
    """A dirty checkout that blocks the fast-forward says so, rather than naming 0 commits.

    `_replay_our_unpushed_commits` is reached on *any* failed fast-forward, and a person's
    uncommitted edit in the notes clone is one of them. The arithmetic then reported
    `0 local commit(s) this system did not write` — a refusal naming commits that do not exist,
    which sends an operator looking for a rebase problem instead of at their own working tree.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    note = work / "knowledge" / "job-result" / "job-x.md"
    note.parent.mkdir(parents=True)
    note.write_text("original body\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "seed note"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)

    other = _clone(remote, tmp_path / "other")
    (other / "knowledge" / "job-result" / "job-x.md").write_text("elsewhere\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "commit", "-q", "-am", "other pod"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)
    # A person editing the notes clone by hand, uncommitted — the fast-forward cannot proceed.
    note.write_text("a person was editing this\n", encoding="utf-8")

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitRemoteError, match="holds no local commits to replay") as raised:
        asyncio.run(writer.write(_note_write("job-y", content="Y\n")))
    assert "0 local commit(s)" not in str(raised.value)


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
                        content=(
                            "---\nid: job-xyz\ntype: job-result\ncreated_by: agent\n---\nbody\n"
                        ),
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

    # **Cloned before the first write, so it is genuinely behind.** Cloning it afterwards is what
    # this test used to do, and it made the fast-forward inert: the second clone already held
    # everything the first had pushed, so removing `--ff-only` from the writer left this test
    # green. Stale, the same removal fails the push as a non-fast-forward.
    work_b = _clone(remote, tmp_path / "fresh")
    asyncio.run(submitter_a.write(v1))

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
    already-tracked changes, no pathspec) instead of the file it names — nothing new gets staged,
    `_write_and_commit`'s idempotence check trips, and the write reports the unchanged tree as if
    it had succeeded while the note is never committed or pushed.
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


def test_a_write_refuses_the_checkout_the_process_runs_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitter pointed at the process's own checkout is refused before any git op.

    The reason has changed twice and the guard has not. It used to protect uncommitted work from
    the `reset --hard` + `clean -fd` every submission ran, then the force-push of a `note/<id>`
    branch. Neither happens now, and what remains is the plainest form of it: a write commits into
    the tree it is handed and pushes that tree's origin, so pointed at the ChemClaw source checkout
    — which the `note_repo_dir="."` default resolves to — it would commit into the running
    application's own source tree and publish the note into the code repository.

    Asserted as the absence of the mutation rather than as an exception alone: the refusal must
    come *before* anything is written, so the checkout holds no new file and no new commit.

    Two assertions used to stand here instead — no `note/*` branch, no `.git/chemclaw-worktrees`
    — and both were unconditionally true, because nothing in this code creates either any more. An
    assertion that cannot fail is a claim that a control exists.
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

    # Nothing ran. (The untracked file surviving is no longer evidence of anything — the writer
    # could not destroy it even without the guard.) What *would* have happened without the guard is
    # a file on disk and a commit on HEAD, so both are asserted: this is the tree the note would
    # have landed in.
    assert uncommitted.read_text(encoding="utf-8") == "do not destroy\n"
    assert not (work / "knowledge" / "job-result" / "job-own.md").exists()
    log = subprocess.run(
        ["git", "-C", str(work), "log", "--oneline", "--grep", "job-own"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert log.strip() == ""


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


def test_no_connector_bundle_can_reach_the_note_write_path() -> None:
    """A bundle *builds* a note; core writes it. Structurally, rather than by convention.

    A bundle used to own a `write_knowledge_node` activity calling `record_note` directly, which
    made "core owns the write path" something the bundle chose to honour rather than a boundary it
    could not cross. Core writes the envelope's note now, so this is what keeps it that way.
    `chemclaw.connectors -> chemclaw.kg` is an allowed edge in `tests/test_layering.py` — bundles
    legitimately build `Note` objects — so this is the rule that narrows that edge to *building*.

    **Two spellings of the same reach used to walk past it.** The scan matched a bare
    `ast.Name` and an import of `kg.pr_gate` — a module that no longer exists, so half of it could
    never fire again — which left `import chemclaw.kg.record as r; r.record_note(...)` and
    `from chemclaw.kg.record import record_note` both green. It now asserts over the *write
    surface* by name rather than over one call spelling: any import that binds a writing name, and
    any attribute access ending in one.
    """
    #: The names that reach the graph. `default_writer` is here beside `record_note` because
    #: constructing the writer is the other half of the same reach — a bundle holding one can
    #: call `write` on it without `record_note` ever appearing.
    forbidden = {"record_note", "default_writer", "GitNoteWriter"}
    bundles = Path("src/chemclaw/connectors")
    offenders = []
    for path in sorted(bundles.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Imports, names and attributes — a *docstring* naming the write path is the point being
        # made, not a violation of it, and `connectors/manifest.py` makes exactly that point.
        for node in ast.walk(tree):
            reached = (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("chemclaw.kg")
                and any(alias.name in forbidden for alias in node.names)
            ) or (
                isinstance(node, ast.Import)
                and any(
                    alias.name in {"chemclaw.kg.record", "chemclaw.kg.git_writer"}
                    for alias in node.names
                )
            )
            named = isinstance(node, ast.Name) and node.id in forbidden
            attributed = isinstance(node, ast.Attribute) and node.attr in forbidden
            if reached or named or attributed:
                offenders.append(str(path.relative_to("src")))
                break
    assert offenders == [], (
        f"{offenders} reach the note write path from inside a connector: a bundle returns its "
        "note in the job envelope and core writes it"
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


# --- what the deep review of D-2026-09-05-the-gate-is-deleted-not-dormant found ----------------


def test_a_push_that_failed_is_pushed_by_the_next_attempt_of_the_same_note(tmp_path: Path) -> None:
    """A stranded commit must not be swallowed by the idempotence that makes a re-record cheap.

    Measured on the first version of this writer: a transient push rejection left the commit on the
    local base branch; the retry rewrote byte-identical content, staged nothing, and returned
    `written=False` **without pushing** — so the note was on one pod's disk, reported to the chemist
    as recorded, and on no remote. `_push` therefore decides by whether the local base is ahead of
    its remote-tracking ref, not by whether this call staged anything.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    with pytest.raises(GitWriteError, match="push"):
        asyncio.run(writer.write(_note_write("job-stranded")))
    hook.unlink()

    outcome = asyncio.run(writer.write(_note_write("job-stranded")))
    assert outcome.written is True, "the retry must push the commit the failed attempt left behind"
    on_remote = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "knowledge/job-result/job-stranded.md" in on_remote


def test_an_agent_write_may_not_overwrite_a_note_a_human_authored(tmp_path: Path) -> None:
    """The control the PR-gate used to be, and the one the deletion ADR did not account for.

    `record_note` checks `created_by` on the note it is handed, which says nothing about what is
    already at that path. Under the gate a reviewer saw "this modifies a human-authored file" in the
    diff; nothing sees it now, so the writer refuses. The ADR's replacement control is a note that
    *contradicts* curated knowledge — which only works while the thing to contradict still exists.
    """
    _, work = _make_remote_and_clone(tmp_path)
    curated = work / "knowledge" / "playbook" / "playbook-suzuki.md"
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "---\nid: playbook-suzuki\ntype: playbook\ncreated_by: human\n---\nPd(dppf)Cl2, 2-MeTHF.\n",
        encoding="utf-8",
    )
    for command in (["add", "-A"], ["commit", "-qm", "curated"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="authored by a human"):
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path="knowledge/playbook/playbook-suzuki.md",
                            content="---\nid: playbook-suzuki\ntype: playbook\n"
                            "created_by: agent\n---\nPdCl2, DMF.\n",
                        )
                    ],
                    message="Add playbook note: playbook-suzuki",
                )
            )
        )
    assert "2-MeTHF" in curated.read_text(encoding="utf-8"), "the chemist's note is untouched"


def test_a_retirement_of_a_persons_note_is_dropped_and_the_new_note_still_lands(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal above applies to the subject note, and must not take the unit down with it.

    `record_failure` puts the `failure-mode` note and the retirement of what it refutes in **one**
    `NoteWrite`, and the writer validated every file before writing any — so refusing the
    retirement discarded the observation as well. That is the shipped case rather than an edge:
    every `playbook` a refutation exists to refute is human-authored, and the refusal even told
    the caller to "record a new note that contradicts it instead", which is exactly what it had
    just thrown away.

    So the amendment steps aside and says so, leaving what `close_refuted_note` documents as the
    truthful state for a claim this system may not close: the curated note stays open and served,
    and the new note marks it as contradicted.
    """
    _, work = _make_remote_and_clone(tmp_path)
    curated = work / "knowledge" / "playbook" / "playbook-suzuki.md"
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "---\nid: playbook-suzuki\ntype: playbook\ncreated_by: human\n---\nPd(dppf)Cl2, 2-MeTHF.\n",
        encoding="utf-8",
    )
    for command in (["add", "-A"], ["commit", "-qm", "curated"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    failure = Note(
        id="failure-suzuki-degassing",
        type="failure-mode",
        created_by="agent",
        body="[[contradicts:playbook-suzuki]] did not hold: the coupling stalled at 12%.",
    )
    retirement = Note(
        id="playbook-suzuki",
        type="playbook",
        created_by="human",
        valid_to=date(2026, 3, 1),
        body="Pd(dppf)Cl2, 2-MeTHF.\n\nRefuted by [[failure-suzuki-degassing]].",
    )
    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_writer"):
        reference = asyncio.run(
            record_note(failure, writer, knowledge_dir="knowledge", superseded=[retirement])
        )

    assert len(reference) == 40, "the failure note landed in a commit of its own"
    landed = work / "knowledge" / "failure-mode" / "failure-suzuki-degassing.md"
    assert landed.exists(), "the observation is the highest-value half and must survive"
    curated_now = curated.read_text(encoding="utf-8")
    assert "valid_to" not in curated_now, "the chemist's note keeps its open validity window"
    assert "2-MeTHF" in curated_now
    assert any("amendment_left_alone" in record.message for record in caplog.records), (
        "dropping a person's retirement silently would be the other half of the same defect"
    )


def test_a_note_is_replaced_in_one_step_so_a_reader_never_sees_half_of_it(tmp_path: Path) -> None:
    """`write_text` truncates then writes; readers of this tree hold no lock.

    Measured on the first version: ~80% of reads overlapping a rewrite saw a partial file, and a
    note whose frontmatter survived the cut *parses cleanly* with half its body — so its wikilinks
    go missing from the graph rather than the file being skipped. Asserted here at the mechanism
    rather than by racing threads, because a race that passes once proves nothing.
    """
    target = tmp_path / "note.md"
    target.write_text("---\nid: n\ntype: reaction\ncreated_by: agent\n---\nold\n", encoding="utf-8")
    inode_before = target.stat().st_ino

    _replace_atomically(target, "---\nid: n\ntype: reaction\ncreated_by: agent\n---\nnew\n")

    assert "new" in target.read_text(encoding="utf-8")
    assert target.stat().st_ino != inode_before, "the file was replaced, not truncated in place"
    assert not list(tmp_path.glob(".note.md.*")), "no temporary file is left behind"


def test_a_no_op_rewrite_beside_a_stray_stage_is_a_no_op_and_not_an_error(tmp_path: Path) -> None:
    """The idempotence check is scoped to *our* paths, and unscoped it fails loudly.

    `test_poisoned_index_does_not_leak_into_the_next_write` covers the commit limiter; this covers
    the `diff --cached` above it, which nothing reached. The combination that separates them is a
    **byte-identical re-write** with something else already staged: scoped, git stages nothing and
    the write returns `written=False`. Unscoped, the stray makes `diff --cached` report a change,
    the path-limited `git commit` then finds nothing to commit on those paths and exits non-zero,
    and the caller gets a non-retryable `GitWriteError` — `durable/publish.py` drops the note.

    (The comment in `git_writer` used to predict the other failure — "would turn a no-op into a
    commit". It cannot: the commit is path-limited too. Measured, and the comment now says so.)
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    note = _note_write("job-idem", content="stable\n")
    assert asyncio.run(writer.write(note)).written is True

    stray = work / "unrelated.txt"
    stray.write_text("somebody else's staged work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", str(stray)], check=True)

    outcome = asyncio.run(writer.write(note))
    assert outcome.written is False, "a byte-identical re-write is a no-op, not a commit"
    staged = subprocess.run(
        ["git", "-C", str(work), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged.strip() == "unrelated.txt", "the stray is neither committed nor discarded"


def _diverge(remote: Path, tmp_path: Path, name: str) -> None:
    """Push a note to `remote` from a third clone, so the pod's own clone is now behind."""
    other = _clone(remote, tmp_path / f"other-{name}")
    note = other / "knowledge" / "job-result" / f"{name}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"from elsewhere: {name}\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-qm", f"elsewhere {name}"], ["push", "-q"]):
        subprocess.run(["git", "-C", str(other), *command], check=True)


def test_a_failed_push_does_not_wedge_every_later_write_on_this_pod(tmp_path: Path) -> None:
    """A stranded commit plus a moved remote must resolve, not fail forever.

    **The two halves of this module contradicted each other and the suite held both.** A push that
    fails deliberately leaves the note committed locally (asserted above). `_push`'s docstring then
    says the next attempt "fetches, fast-forwards past whatever landed, and pushes this commit
    along with its own" — which `--ff-only` cannot do once the remote has moved, because the clone
    has diverged. So the *first* failed push wedged the pod: every later write raised
    `GitRemoteError` on the fast-forward, forever, and no amount of retrying reached the push.

    Driven the way it happens: a push fails, somebody else pushes, and the next write must land
    both notes on the remote.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="push"):
        asyncio.run(writer.write(_note_write("job-stranded", content="stranded\n")))

    hook.unlink()
    _diverge(remote, tmp_path, "job-elsewhere")

    assert asyncio.run(writer.write(_note_write("job-next", content="next\n"))).written is True
    on_remote = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for note in ("job-stranded", "job-elsewhere", "job-next"):
        assert f"knowledge/job-result/{note}.md" in on_remote, f"{note} never reached the remote"


def test_a_persons_local_commit_is_never_replayed(tmp_path: Path) -> None:
    """The refusal the fast-forward was really protecting, kept and made precise.

    Rebasing is safe only while every replayed commit is this writer's own — unpushed and carrying
    `_RECORD_TRAILER`. A commit somebody made by hand in the notes clone is not, and moving it is
    not a decision this writer takes on its own. The old `--ff-only`-and-raise refused this case
    correctly and refused the recoverable one above identically, which is why it read as safe.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    by_hand = work / "knowledge" / "job-result" / "job-by-hand.md"
    by_hand.parent.mkdir(parents=True, exist_ok=True)
    by_hand.write_text("a person wrote this here\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-qm", "a person's commit"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)
    _diverge(remote, tmp_path, "job-elsewhere")

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitRemoteError, match="this system did not write"):
        asyncio.run(writer.write(_note_write("job-next")))
    assert by_hand.read_text(encoding="utf-8") == "a person wrote this here\n"


@pytest.mark.parametrize(
    "relative",
    [".git/config", ".git/hooks/pre-commit", "knowledge/../.git/config"],
)
def test_a_note_path_may_not_reach_into_the_git_directory(tmp_path: Path, relative: str) -> None:
    """`.git/` is inside the checkout, so containment alone lets a note write repository metadata.

    `_contained_note_path` refuses what escapes `root`; every path here resolves *within* it.
    `.git/config` decides where this checkout pushes, `.git/hooks/pre-commit` runs on the next
    commit, and `.git/chemclaw-submit.lock` is the lock guarding the very write doing it.

    Nothing reaches that function with such a path today — `record._note_file` builds every one
    through `note_relative_path`, whose segments are slug-validated — which is the argument that
    made containment "defense in depth" in the first place. Depth that stops one directory short of
    the interesting one is not depth, so the third case is here too: the traversal is *resolved*,
    so a path that climbs out of the knowledge tree and back into `.git` is the same path.
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="git directory"):
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[NoteFile(path=relative, content="url = git@evil.example:x/y.git\n")],
                    message="Add job-result note: job-x",
                )
            )
        )
    assert "evil.example" not in (work / ".git" / "config").read_text(encoding="utf-8")
