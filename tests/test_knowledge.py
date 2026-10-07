"""Tests for `GitNoteWriter`, the one path a note takes from this system into the graph.

A bundle builds a note and core writes it (`tests/test_connector_job_workflow.py` covers that
half); the last test here asserts no bundle has a second way in.
"""

import ast
import asyncio
import logging
import os
import re
import stat
import subprocess
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.kg import git_writer
from chemclaw.kg.git_writer import (
    GitNoteWriter,
    GitRemoteError,
    GitWriteError,
    _replace_atomically,
)
from chemclaw.kg.graph import invalidate_cache, load_notes, note_file_fingerprints
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
    # Point the bare remote's HEAD at `main` so a fresh clone checks out the base branch; the writer
    # commits on the base branch and refuses a clone parked elsewhere.
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
    """A write commits the note on the base branch and pushes.

    The reference returned is the commit, since there is nothing to review or merge.
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
    """The checkout stays on `base`, and the note is readable there.

    `settings.notes_path` resolves to this tree, so the note is visible to readers as soon as it is
    written.
    """
    _, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    asyncio.run(writer.write(_note_write("job-abc")))

    assert _current_branch(work) == "main"
    assert (work / "knowledge" / "job-result" / "job-abc.md").exists()


def test_a_rejected_push_still_leaves_the_checkout_on_base(tmp_path: Path) -> None:
    """A rejected push leaves the checkout on its base branch, with the note committed locally.

    The next successful write carries the local commit.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="push"):
        asyncio.run(writer.write(_note_write("job-unreviewed")))

    assert _current_branch(work) == "main"
    # The note is on disk and committed locally: readable here, absent from the remote until the
    # next successful write fast-forwards and carries it. What must not happen is a silent success.
    assert (work / "knowledge" / "job-result" / "job-unreviewed.md").exists()


def test_a_failure_before_the_commit_leaves_no_note_in_the_tree(tmp_path: Path) -> None:
    """A write that dies on any file leaves none of them in the tree readers scan.

    A write carries a note and its dependencies, so it can fail part-way (here on the second file's
    containment check). Paths are resolved and checked before any byte lands, and anything already
    written is restored if a later step raises.
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
    """A commit that fails after `git add` un-stages what it staged, so the pod can write again.

    A failing `pre-commit` hook stands in for an `index.lock` or a timeout kill. If the index kept
    the retracted blob, the content would sit staged in the shared clone and every later `merge
    --ff-only` would refuse, wedging the pod.
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

    `_replay_our_unpushed_commits` runs on any failed fast-forward, and an uncommitted edit in the
    clone must be reported as such so the operator looks at the working tree.
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
    """A write busts readers' graph caches, because it touches their tree.

    A surviving cache would serve a graph missing the note just recorded for up to
    `graph_cache_ttl_seconds`.
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

    Both target one branch and one working tree, so unserialized they would stage each other's files
    or race the same push.
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


def test_two_sequential_event_loops_can_both_write_concurrently(tmp_path: Path) -> None:
    """The write lock survives a second event loop in the same process.

    A module-level `asyncio.Lock` binds to the first loop that contends on it, so a second
    `asyncio.run` in one process (as `mutmut`'s `pytest.main()` runs do) would hang. Two loops, each
    contending, since an uncontended acquire takes the fast path and binds nothing.
    `tests/test_loop_local_locks.py` covers the mechanism.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    async def both(suffix: str) -> tuple[str, str]:
        ref_a, ref_b = await asyncio.gather(
            writer.write(_note_write(f"job-{suffix}-a", content=f"note {suffix} a\n")),
            writer.write(_note_write(f"job-{suffix}-b", content=f"note {suffix} b\n")),
        )
        return ref_a.reference, ref_b.reference

    for loop_number, suffix in enumerate(("first", "second"), start=1):
        refs = asyncio.run(both(suffix))
        assert len(set(refs)) == 2, f"loop {loop_number} produced one commit for two writes: {refs}"

    files = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for suffix in ("first", "second"):
        for half in ("a", "b"):
            assert f"knowledge/job-result/job-{suffix}-{half}.md" in files, (
                f"job-{suffix}-{half} never reached the remote, so one loop's writes were lost: "
                f"{files}"
            )


def test_second_process_holding_the_checkout_is_rejected(tmp_path: Path) -> None:
    """A submit against a checkout flocked by another process fails fast, then recovers.

    Cross-process ownership of `note_repo_dir` is an exclusive `flock` on
    `.git/chemclaw-submit.lock`. A child process takes it; the submit raises `GitWriteError`, then
    succeeds once it is released.
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
    """The flock does not outlive a write that errored.

    The failure is forced by naming a base branch the checkout is not on, which `_write_locked`
    checks inside the lock, so this tests the release.
    """
    _, work = _make_remote_and_clone(tmp_path)
    bad = GitNoteWriter(repo_dir=str(work), base_branch="no-such-base", remote="origin")
    with pytest.raises(GitWriteError, match="not the base branch"):
        asyncio.run(bad.write(_note_write("job-x")))

    good = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    assert asyncio.run(good.write(_note_write("job-x"))).written is True


def test_rewriting_a_note_from_a_second_clone_lands_on_the_shared_base(tmp_path: Path) -> None:
    """A second clone recording a newer version of the same note replaces it on the shared base.

    Two clones write the same branch, so the second must fast-forward onto what the first pushed
    before committing.
    """
    remote, work_a = _make_remote_and_clone(tmp_path)
    v1 = _note_write("job-x", content="v1\n")
    submitter_a = GitNoteWriter(repo_dir=str(work_a), base_branch="main", remote="origin")

    # Cloned before the first write, so it is genuinely behind; cloned after, the fast-forward would
    # be inert and removing `--ff-only` would stay green.
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
    """A note path starting with `-` reaches `git add` as a pathspec, not an option.

    `repo_root / "-u"` passes containment; without `--`, git reads `-u` as `--update`, stages
    nothing, and the idempotence check reports success while the note is never committed.
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
    """A write refuses the checkout the process runs from, before any git operation.

    A write commits into the tree it is handed and pushes its origin, so pointed at the
    application's own source checkout (the `note_repo_dir="."` default) it would publish the note
    into the code repository. Asserted as the absence of the mutation: no new file and no new
    commit.
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

    # Nothing ran: without the guard there would be a file on disk and a commit on HEAD, so both are
    # asserted absent.
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

    There is one index, so `_write_and_commit` passes `-- <written paths>` and scopes the
    idempotence check the same way. The stray stays staged afterwards: it is not this writer's to
    discard.
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

    Containment is checked against the tree after the base branch is materialized; on an
    unmaterialized tree there would be no symlink to resolve and the check would pass vacuously.
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


async def test_a_cancelled_git_read_kills_its_child_like_every_other_git_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled git read kills its child like every other git command.

    `_run` owns the timeout and kill-on-cancel for every git command; `_read` must too, or a
    submission cancelled mid-read (a Temporal activity timeout) leaves the `git rev-parse`/`git log`
    child running. Driven on `_read` directly.
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

    reading = asyncio.create_task(writer._read("refs/remotes/origin/note/x"))
    await asyncio.sleep(0.05)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading

    assert killed["value"] is True, (
        "a cancelled `_read` left its git child running — the orphan `_run` has an arm for"
    )


def test_no_connector_bundle_can_reach_the_note_write_path() -> None:
    """No connector bundle can reach the note write path.

    `chemclaw.connectors -> chemclaw.kg` is an allowed layering edge so bundles can build `Note`
    objects; this narrows it to building. Asserted over the write surface by name: any import
    binding a writing name and any attribute access ending in one, so aliased spellings are caught.
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

    A machine-rendered compound note rides along with every note linking it and must not revert a
    chemist's edit.
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

    A remote, credential helper or hook must not see the LLM key, a DSN or the framing HMAC. The
    notes-remote token and PATH survive, which keeps `push` working.
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


_ASKPASS = Path(__file__).resolve().parents[1] / "deploy" / "git-askpass.sh"
_ASKPASS_IN_IMAGE = "/usr/local/bin/chemclaw-git-askpass"


def _basic_auth_git_remote(root: Path, token: str) -> tuple[str, ThreadingHTTPServer]:
    """Serve `root` as a smart-HTTP git remote that refuses every request without `token`.

    `git http-backend` behind a Basic-auth check — the shape of a private HTTPS knowledge repo,
    minus TLS, which is not what this asks about.
    """
    import base64
    import threading

    expected = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()

    class _Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="notes"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            cgi_env = {
                **os.environ,
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "REQUEST_METHOD": self.command,
                "REMOTE_USER": "notes",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
            }
            out = subprocess.run(
                ["git", "http-backend"], input=body, env=cgi_env, capture_output=True, check=True
            ).stdout
            head, _, rest = out.partition(b"\r\n\r\n")
            status, headers = 200, []
            for line in head.split(b"\r\n"):
                key, _, value = line.decode().partition(": ")
                if key.lower() == "status":
                    status = int(value.split()[0])
                elif key:
                    headers.append((key, value))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(rest)))
            self.end_headers()
            self.wfile.write(rest)

        do_GET = do_POST = _serve

        def log_message(self, *_args: object) -> None:
            """Keep the test's output to its own assertions."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/notes.git", server


def test_the_note_writer_can_push_to_a_remote_that_needs_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note writer can push to a remote that needs the token, through the image's askpass
    helper.

    `knowledge-sync.sh checkout` leaves a remote URL without the token, and the push runs in another
    container. Driven against a Basic-auth remote: the push succeeds once `GIT_ASKPASS` names
    `deploy/git-askpass.sh`, which `deploy/entrypoint.sh` arms in every application container. The
    token ends up in neither the clone's config nor its remote URL.
    """
    if subprocess.run(["git", "http-backend"], capture_output=True, env={}).returncode == 127:
        pytest.skip("git http-backend is not installed")
    from chemclaw.kg.git_writer import _git_child_env

    token = "notes-push-token"
    bare = tmp_path / "notes.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True)
    url, server = _basic_auth_git_remote(tmp_path, token)
    try:
        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.org"]
        subprocess.run(
            ["git", "-C", str(seed), *identity, "commit", "-q", "--allow-empty", "-m", "seed"],
            check=True,
        )
        subprocess.run(["git", "-C", str(seed), "push", "-q", str(bare), "main"], check=True)

        monkeypatch.setenv("CHEMCLAW_KNOWLEDGE_REPO_TOKEN", token)
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
        monkeypatch.delenv("GIT_ASKPASS", raising=False)
        monkeypatch.delenv("SSH_ASKPASS", raising=False)
        clone = tmp_path / "checkout"
        subprocess.run(
            ["git", "clone", "-q", url, str(clone)],
            check=True,
            env={**os.environ, "GIT_ASKPASS": str(_ASKPASS)},
        )
        subprocess.run(
            ["git", "-C", str(clone), *identity, "commit", "-q", "--allow-empty", "-m", "note"],
            check=True,
        )

        def _push() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(clone), "push", "origin", "main"],
                env=_git_child_env(),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )

        refused = _push()
        assert refused.returncode != 0, "the push succeeded with no credential path; probe broken"
        assert "Username" in refused.stderr, refused.stderr

        monkeypatch.setenv("GIT_ASKPASS", str(_ASKPASS))
        pushed = _push()
        assert pushed.returncode == 0, pushed.stderr
        assert token not in pushed.stderr
    finally:
        server.shutdown()
    assert token not in (clone / ".git" / "config").read_text(encoding="utf-8")


def test_every_container_that_runs_git_against_the_notes_remote_arms_the_same_askpass() -> None:
    """Every container that runs git against the notes remote arms the same askpass helper.

    The Containerfile installs `deploy/git-askpass.sh` at the path both `deploy/entrypoint.sh` and
    `deploy/knowledge-sync.sh` arm; the three are read against each other.
    """
    deploy = _ASKPASS.parent
    containerfile = (deploy / "Containerfile").read_text(encoding="utf-8")
    assert f"COPY deploy/git-askpass.sh {_ASKPASS_IN_IMAGE}" in containerfile
    for script in ("entrypoint.sh", "knowledge-sync.sh"):
        text = (deploy / script).read_text(encoding="utf-8")
        armed = rf'export GIT_ASKPASS="\$\{{[A-Z_]+:-{re.escape(_ASKPASS_IN_IMAGE)}\}}"'
        assert re.search(armed, text), (
            f"deploy/{script} does not arm {_ASKPASS_IN_IMAGE} as GIT_ASKPASS"
        )
    answers = {
        prompt: subprocess.run(
            ["bash", str(_ASKPASS), prompt],
            env={"CHEMCLAW_KNOWLEDGE_REPO_TOKEN": "tok", "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for prompt in ("Username for 'https://git.example.org': ", "Password for 'https://x': ")
    }
    assert answers == {
        "Username for 'https://git.example.org': ": "x-access-token\n",
        "Password for 'https://x': ": "tok\n",
    }


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
    """A push that failed is pushed by the next attempt of the same note.

    A byte-identical retry stages nothing, so `_push` decides by whether the local base is ahead of
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
    """An agent write may not overwrite a note a human authored.

    `record_note` checks `created_by` on the incoming note, which says nothing about the file
    already at that path, so the writer checks. Contradiction only works while the curated note
    still exists.
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
    """A retirement of a person's note is dropped, and the new note still lands.

    `record_failure` puts the failure note and the retirement in one `NoteWrite`; refusing the
    retirement must not discard the observation. The curated note stays open and served, and the new
    note marks it as contradicted, as `close_refuted_note` documents.
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
    """A note is replaced in one step, so a reader never sees half of it.

    Readers hold no lock, and a truncated note whose frontmatter survives parses cleanly with half
    its body. Asserted at the mechanism (atomic replace) rather than by racing threads.
    """
    target = tmp_path / "note.md"
    target.write_text("---\nid: n\ntype: reaction\ncreated_by: agent\n---\nold\n", encoding="utf-8")
    inode_before = target.stat().st_ino

    _replace_atomically(target, b"---\nid: n\ntype: reaction\ncreated_by: agent\n---\nnew\n")

    assert "new" in target.read_text(encoding="utf-8")
    assert target.stat().st_ino != inode_before, "the file was replaced, not truncated in place"
    assert not list(tmp_path.glob(".note.md.*")), "no temporary file is left behind"


def test_a_replacement_keeps_the_notes_permissions_rather_than_the_temporary_files(
    tmp_path: Path,
) -> None:
    """A replacement keeps the note's permissions rather than the temporary file's.

    `NamedTemporaryFile` creates 0600, and `os.replace` would carry that onto a note in a checkout
    the sync sidecar and every reader use.
    """
    existing = tmp_path / "existing.md"
    existing.write_text("old\n", encoding="utf-8")
    existing.chmod(0o644)

    _replace_atomically(existing, b"new\n")

    assert stat.S_IMODE(existing.stat().st_mode) == 0o644, "an existing mode is preserved"

    fresh = tmp_path / "fresh.md"
    saved = os.umask(0o022)
    try:
        _replace_atomically(fresh, b"new\n")
    finally:
        os.umask(saved)

    assert stat.S_IMODE(fresh.stat().st_mode) == 0o644, (
        "and a new note gets what `open()` would have given it under this umask, not 0600"
    )


def test_a_no_op_rewrite_beside_a_stray_stage_is_a_no_op_and_not_an_error(tmp_path: Path) -> None:
    """A no-op rewrite beside a stray stage is a no-op, not an error.

    The idempotence check (`diff --cached`) is scoped to this writer's paths. Unscoped, the stray
    would report a change, the path-limited commit would find nothing and fail, and the caller would
    get a non-retryable `GitWriteError` that drops the note.
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
    """A failed push does not wedge every later write on this pod.

    After a failed push leaves a local commit and someone else pushes, `--ff-only` cannot proceed,
    so the writer replays its own unpushed commits. Driven as it happens: a push fails, another
    pushes, and the next write must land both notes on the remote.
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


def test_a_clone_with_no_identity_of_its_own_still_records_and_replays_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone with no identity of its own still records and replays notes.

    A deployed clone has no `user.*` and no global config, and git refuses its hostname guess, so
    the writer states the commit identity itself. The environment here has no identity source at
    all, with `user.useConfigOnly` set; the arm first proves a plain commit fails. Both the commit
    and the replay rebase (which sets a committer) are driven, with non-default settings to show the
    identity comes from config.
    """
    remote, _ = _make_remote_and_clone(tmp_path)
    work = tmp_path / "bare-identity"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)

    empty_home = tmp_path / "home"
    empty_home.mkdir()
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("EMAIL", raising=False)
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")

    control = subprocess.run(
        ["git", "-C", str(work), "commit", "--allow-empty", "-qm", "who am I"],
        capture_output=True,
        text=True,
    )
    assert control.returncode != 0 and "identity" in control.stderr.lower(), (
        "a plain commit succeeded in this environment, so it still supplies an identity from "
        f"somewhere and the writer's arm below proves nothing: {control.stderr!r}"
    )

    monkeypatch.setattr(settings, "note_committer_name", "Notes Service")
    monkeypatch.setattr(settings, "note_committer_email", "notes@site.invalid")
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    first = asyncio.run(writer.write(_note_write("job-no-identity", content="first\n")))
    assert first.written is True

    # Strand the next note locally, move the remote, and let the write after it replay the note.
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    with pytest.raises(GitWriteError, match="push"):
        asyncio.run(writer.write(_note_write("job-stranded", content="stranded\n")))
    hook.unlink()
    _diverge(remote, tmp_path, "job-elsewhere")
    assert asyncio.run(writer.write(_note_write("job-after", content="after\n"))).written is True

    idents = subprocess.run(
        ["git", "-C", str(remote), "log", "main", "--format=%s|%an <%ae>|%cn <%ce>"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    ours = [line for line in idents if line.startswith("Add job-result note:")]
    assert len(ours) == 3, idents
    for line in ours:
        _subject, author, committer = line.split("|")
        assert author == committer == "Notes Service <notes@site.invalid>", line


def test_a_persons_local_commit_is_never_replayed(tmp_path: Path) -> None:
    """A person's local commit is never replayed.

    Rebasing is safe only while every replayed commit is this writer's own (unpushed, carrying
    `_RECORD_TRAILER`); a hand-made commit in the clone is refused.
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
    """A note path may not reach into the git directory.

    `.git/` is inside the checkout, so containment alone would allow writing `.git/config`, a hook,
    or the submit lock. Paths are resolved first, so a traversal that climbs back into `.git` is the
    same path.
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


def _stage_without_committing(work: Path, relative: str, content: str) -> None:
    """Leave `relative` written and staged, exactly as a `SIGKILL` between add and commit does.

    Built with plain git because a test may not kill the process running it.
    """
    path = work / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "--", relative], check=True)


def test_a_hard_kill_between_add_and_commit_does_not_wedge_the_pod_forever(
    tmp_path: Path,
) -> None:
    """A hard kill between add and commit does not wedge the pod forever.

    The in-process rollback does not run on an OOMKill or eviction, leaving blobs staged. Without
    recovery, another pod pushing the same paths makes every later write here fail, and the
    sidecar's `--ff-only` cannot clear it either.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    _stage_without_committing(work, "knowledge/job-result/job-killed.md", "half a write\n")
    _diverge(remote, tmp_path, "job-killed")

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    outcome = asyncio.run(writer.write(_note_write("job-after", content="after\n")))

    assert outcome.written is True, "a later write must not inherit the dead write's index"
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == "", f"the killed write's residue survived: {status!r}"
    # The residue is *gone*, not committed: the dead write was retracted, not published.
    assert (work / "knowledge" / "job-result" / "job-killed.md").read_text(
        encoding="utf-8"
    ) == "from elsewhere: job-killed\n"
    on_remote = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "knowledge/job-result/job-after.md" in on_remote


def test_a_staged_note_survives_every_write_the_checkout_can_still_serve(tmp_path: Path) -> None:
    """A staged note survives every write the checkout can still serve.

    Staged residue is indistinguishable from an operator's staged work, so recovery runs only when
    the checkout cannot move; while it can, nothing is discarded across three further writes.
    """
    _, work = _make_remote_and_clone(tmp_path)
    _stage_without_committing(work, "knowledge/job-result/job-staged.md", "an operator's work\n")
    stray = work / "unrelated.txt"
    stray.write_text("somebody else's staged work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "--", "unrelated.txt"], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    for index in range(3):
        assert asyncio.run(writer.write(_note_write(f"job-after-{index}"))).written is True

    staged = subprocess.run(
        ["git", "-C", str(work), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert sorted(staged.split()) == ["knowledge/job-result/job-staged.md", "unrelated.txt"]
    assert stray.read_text(encoding="utf-8") == "somebody else's staged work\n"


def test_a_blocker_outside_the_knowledge_tree_is_still_reported_rather_than_discarded(
    tmp_path: Path,
) -> None:
    """A blocker outside the knowledge tree is still reported rather than discarded.

    Every path this writer stages is under `<knowledge_dir>/`, so a staged change elsewhere is not
    its residue and keeps the refusal that names it.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    (work / "README.md").write_text("an operator's uncommitted edit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "--", "README.md"], check=True)
    other = _clone(remote, tmp_path / "other-readme")
    (other / "README.md").write_text("from another pod\n", encoding="utf-8")
    for command in (["commit", "-qam", "elsewhere"], ["push", "-q"]):
        subprocess.run(["git", "-C", str(other), *command], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitRemoteError, match="no local commits to replay"):
        asyncio.run(writer.write(_note_write("job-blocked")))
    assert (work / "README.md").read_text(encoding="utf-8") == "an operator's uncommitted edit\n"


def test_a_stale_index_lock_is_cleared_and_a_fresh_one_is_retryable(tmp_path: Path) -> None:
    """A stale index lock is cleared, and a fresh one is retryable.

    A killed `git commit` leaves `.git/index.lock`. Classification: the lock error is retryable, not
    a `GitWriteError` that drops the note. Removal: a stale lock is cleared under the flock (no peer
    writer mid-write), and the age bound covers a person running git in the clone by hand.
    """
    _, work = _make_remote_and_clone(tmp_path)
    lock = work / ".git" / "index.lock"
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    lock.touch()
    with pytest.raises(GitRemoteError, match="index.lock"):
        asyncio.run(writer.write(_note_write("job-fresh-lock")))
    assert lock.exists(), "a lock that may still have a live holder is not removed"

    stale = time.time() - settings.git_command_timeout_seconds - 1
    os.utime(lock, (stale, stale))
    assert asyncio.run(writer.write(_note_write("job-stale-lock"))).written is True
    assert not lock.exists()


@pytest.mark.parametrize(
    "wording",
    [
        "remote: Permission to acme/knowledge.git denied to chemclaw-bot.",
        "remote: You are not allowed to push code to this project.",
        "remote: Write access to repository not granted.",
        "remote: error: 403 ... has not enabled or enforced SAML SSO",
        "remote: TF401027: You need the Git 'GenericContribute' permission.",
    ],
)
def test_a_forge_denying_the_push_is_not_retried_against_the_same_credential(
    tmp_path: Path, wording: str
) -> None:
    """A forge denying the push is not retried against the same credential.

    `_push` routes its failure through `_is_auth_failure`, so a read-only token (fetch succeeds,
    push denied) does not burn `note_write_max_attempts`. Driven through `write()` rather than the
    classifier.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho {wording!r} >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    with pytest.raises(GitWriteError) as raised:
        asyncio.run(writer.write(_note_write("job-denied")))
    assert not isinstance(raised.value, GitRemoteError), (
        "a denied credential must not be retried: no number of retries installs a token"
    )


def test_a_rejected_push_that_is_not_a_denial_is_still_retryable(tmp_path: Path) -> None:
    """A rejected push that is not a denial is still retryable.

    A bare 403 can be a secondary rate limit, so a transient server-side rejection keeps the
    retryable class.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: failed to lock ref' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    with pytest.raises(GitRemoteError, match="push"):
        asyncio.run(writer.write(_note_write("job-blip")))


def test_a_failed_push_tells_the_caller_the_note_is_already_readable_here(tmp_path: Path) -> None:
    """A failed push tells the caller the note is already readable here.

    The bytes land in the scanned tree before the push, so "the write failed" would be false where
    it matters, and a model told so retries or prints the document into chat.
    `surface_domain_errors` shows this text to the model.
    """
    remote, work = _make_remote_and_clone(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")

    with pytest.raises(GitRemoteError) as raised:
        asyncio.run(writer.write(_note_write("job-live", content="live\n")))

    assert (work / "knowledge" / "job-result" / "job-live.md").exists(), (
        "the premise: the note is readable in the tree readers scan"
    )
    message = str(raised.value)
    assert "already" in message and "readable here" in message, message
    assert "re-record nothing" in message, message


def test_the_refusal_names_the_file_the_graph_actually_serves_for_that_id(
    tmp_path: Path,
) -> None:
    """The refusal names the file the graph actually serves for that id.

    With two human claimants, ties go to the first in path order (`campaign` before `playbook`),
    which is the file the graph serves, so the refusal must name it.
    """
    _, work = _make_remote_and_clone(tmp_path)
    for note_type, solvent in (("campaign", "2-MeTHF"), ("playbook", "toluene")):
        curated = work / "knowledge" / note_type / "shared-id.md"
        curated.parent.mkdir(parents=True, exist_ok=True)
        curated.write_text(
            f"---\nid: shared-id\ntype: {note_type}\ncreated_by: human\n---\n"
            f"Pd(dppf)Cl2, {solvent}.\n",
            encoding="utf-8",
        )
    for command in (["add", "-A"], ["commit", "-qm", "curated"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    invalidate_cache()
    served = load_notes(work / "knowledge")
    assert [note.type for note in served] == ["campaign"], (
        "this test's own premise: the graph resolves the tie by keeping the first in path order"
    )

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="authored by a human") as raised:
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path="knowledge/optimization/shared-id.md",
                            content="---\nid: shared-id\ntype: optimization\n"
                            "created_by: agent\n---\nPdCl2, DMF.\n",
                        )
                    ],
                    message="Add optimization note: shared-id",
                )
            )
        )
    assert "knowledge/campaign/shared-id.md" in str(raised.value), (
        "the refusal must name the file the graph serves for that id, which is the first in path "
        f"order — naming the other claimant sends a chemist to a note no query answers with: "
        f"{raised.value}"
    )
    assert "knowledge/playbook/shared-id.md" not in str(raised.value), (
        "and it must name that one only: two paths in one refusal is a reader guessing which"
    )


def test_an_agent_write_may_not_take_the_id_of_a_human_note_filed_under_another_type(
    tmp_path: Path,
) -> None:
    """An agent write may not take the id of a human note filed under another type.

    `graph._parse_notes` keeps the first file in path order, so an agent note at `campaign/<id>.md`
    would displace a curated `playbook/<id>.md` from every query and from `reindex_notes`, without
    touching its file. The check is by id, not path.
    """
    _, work = _make_remote_and_clone(tmp_path)
    curated = work / "knowledge" / "playbook" / "shared-id.md"
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "---\nid: shared-id\ntype: playbook\ncreated_by: human\n---\nPd(dppf)Cl2, 2-MeTHF.\n",
        encoding="utf-8",
    )
    for command in (["add", "-A"], ["commit", "-qm", "curated"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    invalidate_cache()
    assert [note.created_by for note in load_notes(work / "knowledge")] == ["human"]

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError, match="authored by a human") as raised:
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path="knowledge/campaign/shared-id.md",
                            content="---\nid: shared-id\ntype: campaign\n"
                            "created_by: agent\n---\nPdCl2, DMF.\n",
                        )
                    ],
                    message="Add campaign note: shared-id",
                )
            )
        )
    assert "knowledge/playbook/shared-id.md" in str(raised.value), (
        "the refusal must name the file holding the id, not only the path that was refused — "
        f"otherwise a reader cannot act on it: {raised.value}"
    )
    assert not (work / "knowledge" / "campaign" / "shared-id.md").exists()

    invalidate_cache()
    served = load_notes(work / "knowledge")
    assert [note.created_by for note in served] == ["human"], (
        "the chemist's note is still the one the graph serves for this id"
    )
    assert "2-MeTHF" in served[0].body
    assert sorted(note_file_fingerprints(work / "knowledge")) == ["shared-id"], (
        "and the retrieval index still fingerprints the curated file under that id"
    )


def test_a_retirement_of_a_persons_note_under_another_type_is_dropped_not_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A retirement of a person's note under another type is dropped, not refused.

    `record_failure` writes the failure note and the retirement in one `NoteWrite`, so the id-scoped
    check must still let the amendment step aside rather than refuse the whole unit.
    """
    _, work = _make_remote_and_clone(tmp_path)
    curated = work / "knowledge" / "playbook" / "shared-id.md"
    curated.parent.mkdir(parents=True)
    curated.write_text(
        "---\nid: shared-id\ntype: playbook\ncreated_by: human\n---\nPd(dppf)Cl2, 2-MeTHF.\n",
        encoding="utf-8",
    )
    for command in (["add", "-A"], ["commit", "-qm", "curated"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_writer"):
        outcome = asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path="knowledge/failure-mode/failure-shared.md",
                            content="---\nid: failure-shared\ntype: failure-mode\n"
                            "created_by: agent\n---\n[[contradicts:shared-id]] stalled at 12%.\n",
                        ),
                        NoteFile(
                            path="knowledge/campaign/shared-id.md",
                            content="---\nid: shared-id\ntype: campaign\ncreated_by: human\n"
                            "valid_to: 2026-03-01\n---\nPd(dppf)Cl2, 2-MeTHF.\n",
                            amendment=True,
                        ),
                    ],
                    message="Add failure-mode note: failure-shared",
                )
            )
        )

    assert outcome.notes == 1, "the observation is the highest-value half and must survive"
    assert (work / "knowledge" / "failure-mode" / "failure-shared.md").exists()
    assert not (work / "knowledge" / "campaign" / "shared-id.md").exists()
    assert "valid_to" not in curated.read_text(encoding="utf-8")
    assert any("amendment_left_alone" in record.message for record in caplog.records), (
        "dropping a person's retirement silently would be the other half of the same defect"
    )


def test_a_failed_write_restores_every_file_even_one_holding_non_utf8_bytes(
    tmp_path: Path,
) -> None:
    """A failed write restores every file, even one holding non-UTF-8 bytes.

    The rollback restores bytes, not decoded text; decoding would raise inside the handler, skip
    later restores and the un-stage, and replace the real `GitWriteError`. The undecodable file is
    first, so stopping early would leave the second rewritten.
    """
    _, work = _make_remote_and_clone(tmp_path)
    notes = work / "knowledge" / "reaction"
    notes.mkdir(parents=True)
    latin = notes / "latin.md"
    body = "---\nid: latin\ntype: reaction\ncreated_by: agent\n---\nRan at 80\xb0C.\n"
    latin_bytes = body.encode("cp1252")
    latin.write_bytes(latin_bytes)
    other = notes / "other.md"
    other.write_text(
        "---\nid: other\ntype: reaction\ncreated_by: agent\n---\nOriginal other.\n",
        encoding="utf-8",
    )
    for command in (["add", "-A"], ["commit", "-qm", "seed"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    # A failing `pre-commit` hook: the cheapest way to fail *after* the `git add`, which is the
    # arm where the index residue matters.
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with pytest.raises(GitWriteError):
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path="knowledge/reaction/latin.md",
                            content="---\nid: latin\ntype: reaction\n"
                            "created_by: agent\n---\nREWRITTEN.\n",
                        ),
                        NoteFile(
                            path="knowledge/reaction/other.md",
                            content="---\nid: other\ntype: reaction\n"
                            "created_by: agent\n---\nREWRITTEN.\n",
                        ),
                    ],
                    message="Rewrite two notes",
                )
            )
        )

    assert latin.read_bytes() == latin_bytes, "the undecodable note is restored byte for byte"
    assert "Original other" in other.read_text(encoding="utf-8"), (
        "the file after it in the plan is restored too — a rollback that stops at its first "
        "failure publishes the rest of a write that did not land"
    )
    staged = subprocess.run(
        ["git", "-C", str(work), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert staged == [], f"the un-stage after the restores must still run, staged: {staged}"


def test_a_restore_that_fails_does_not_skip_the_remaining_restores_or_the_unstage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A restore that fails does not skip the remaining restores or the un-stage.

    A restore still touches the filesystem (full disk, revoked permission), so the failure is
    injected at the restore itself; injected because this suite runs as root, where `chmod` produces
    no failure.
    """
    _, work = _make_remote_and_clone(tmp_path)
    notes = work / "knowledge" / "reaction"
    notes.mkdir(parents=True)
    first, second = notes / "aaa.md", notes / "bbb.md"
    for path, note_id in ((first, "aaa"), (second, "bbb")):
        path.write_text(
            f"---\nid: {note_id}\ntype: reaction\ncreated_by: agent\n---\nOriginal {note_id}.\n",
            encoding="utf-8",
        )
    for command in (["add", "-A"], ["commit", "-qm", "seed"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(work), *command], check=True)

    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    real = git_writer._replace_atomically
    seen: list[Path] = []

    def failing_on_the_first_restore(path: Path, content: bytes) -> None:
        """Let both forward writes through; raise on the restore of the first planned file."""
        seen.append(path)
        if path == first and seen.count(first) == 2:
            raise OSError(28, "No space left on device")
        real(path, content)

    monkeypatch.setattr(git_writer, "_replace_atomically", failing_on_the_first_restore)

    writer = GitNoteWriter(repo_dir=str(work), base_branch="main", remote="origin")
    with (
        caplog.at_level(logging.ERROR, logger="chemclaw.kg.git_writer"),
        pytest.raises(GitWriteError),
    ):
        asyncio.run(
            writer.write(
                NoteWrite(
                    files=[
                        NoteFile(
                            path=f"knowledge/reaction/{note_id}.md",
                            content=f"---\nid: {note_id}\ntype: reaction\n"
                            "created_by: agent\n---\nREWRITTEN.\n",
                        )
                        for note_id in ("aaa", "bbb")
                    ],
                    message="Rewrite two notes",
                )
            )
        )

    assert "Original bbb" in second.read_text(encoding="utf-8"), (
        "the second file's restore must run even though the first one raised"
    )
    staged = subprocess.run(
        ["git", "-C", str(work), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert staged == [], f"and the index un-stage must still run, staged: {staged}"
    assert any("rollback_failed" in record.message for record in caplog.records), (
        "a file left holding bytes that were never committed has to name itself to an operator"
    )
