"""Can a reader see an unreviewed note while the PR-gate is submitting it? Measured: no.

This is a governance question, not a reliability one. The PR-gate is the review line — the agent
proposes, a human decides (D-005) — and the whole control rests on an agent-authored note being
invisible as *knowledge* until a human merges it.

**It was reachable, and it was measured here first.** `settings.knowledge_path` is
`note_repo_dir / knowledge_dir`, so readers resolve into the same working tree the submitter used
to switch with `git checkout -B note/<id>`; the note was committed into that tree; and
`load_notes` caches for `graph_cache_ttl_seconds`, so a reader that scanned during the window kept
serving the unreviewed note for up to a TTL after the branch was gone. Nothing filtered it:
`created_by == "agent"` is read in exactly one place (`retrieval/harness.py`, to *label* a chunk in
a report) and no reader consults `note_proposals.state`.

The submission now happens in a `git worktree` under `.git/`, which no reader is ever pointed at,
so the shared tree is never switched at all (D-2026-08-05). These tests are the regression targets
for that, and they are a **rewrite** of the ones that pinned the old behaviour rather than a sign
flip of them: three of those four drove raw `git checkout -B` by hand and asserted a property of
*git*, so against the fixed submitter they would have gone green while proving nothing — the exact
failure the file was written to prevent. Every assertion below drives `GitNoteWriter`, and every
absence assertion is paired with a positive one, because "the note is not in the tree" is also true
of a submission that never happened.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from chemclaw.kg.git_writer import GitNoteWriter, _checkout_lock
from chemclaw.kg.graph import invalidate_cache, load_notes
from chemclaw.kg.record import NoteFile, NoteWrite

_UNREVIEWED = "---\nid: agent-proposal\ntype: reaction\ncreated_by: agent\n---\n\nUnreviewed.\n"


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, failing loudly — a silent setup failure fakes a pass."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )
    return result.stdout


def _submission(note_id: str = "agent-proposal") -> NoteWrite:
    """The real submission shape the PR-gate builds: one agent note under `knowledge/<type>/`."""
    return NoteWrite(
        branch=f"note/{note_id}",
        files=[NoteFile(path=f"knowledge/reaction/{note_id}.md", content=_UNREVIEWED)],
        title=f"Add reaction note: {note_id}",
        body="review me",
    )


@pytest.fixture()
def knowledge_clone(tmp_path: Path) -> Path:
    """A real bare remote plus a real working clone with one merged note in it.

    Real git, not a stub: the window under test was created by a working tree being switched, so a
    fake submitter would test the fake. The merged note gives the reader something legitimate to
    see, which is what makes "and also the unreviewed one" a detectable difference.

    The clone carries its own committer identity because the submitter commits inside the worktree
    through `asyncio.create_subprocess_exec` and does not inherit the per-command env `_git` uses.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    notes = clone / "knowledge" / "reaction"
    notes.mkdir(parents=True)
    (notes / "merged-note.md").write_text(
        "---\nid: merged-note\ntype: reaction\ncreated_by: human\n---\n\nA merged note.\n",
        encoding="utf-8",
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "seed")
    _git(clone, "push", "origin", "main")
    return clone


def _submitter(clone: Path) -> GitNoteWriter:
    """A submitter pointed at the clone, with the config defaults the fixture establishes."""
    return GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")


def test_a_reader_never_sees_the_note_at_any_point_during_the_submission(
    knowledge_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline regression target, sampled between every single git command.

    Deterministic — no sleeps, no threads. `_run` is the one door every git command goes through,
    so wrapping it samples the shared tree at every instant a reader could have observed it: what
    `load_notes` returns, whether the file exists, and which branch `HEAD` names.

    The non-vacuity check at the end is the part that makes the absences mean something: the note
    really is committed on `note/agent-proposal`, and a worktree really was created — so the tree
    stayed clean because the work happened elsewhere, not because no work happened.
    """
    notes_dir = knowledge_clone / "knowledge"
    head_file = knowledge_clone / ".git" / "HEAD"
    seen_ids: set[str] = set()
    file_seen: list[bool] = []
    heads: set[str] = set()

    submitter = _submitter(knowledge_clone)
    real_run = submitter._run

    async def _sampling_run(*args: str, cwd: str | None = None) -> tuple[int, str]:
        def _sample() -> None:
            invalidate_cache(notes_dir)
            seen_ids.update(note.id for note in load_notes(notes_dir))
            file_seen.append((notes_dir / "reaction" / "agent-proposal.md").exists())
            heads.add(head_file.read_text(encoding="utf-8").strip())

        _sample()
        try:
            return await real_run(*args, cwd=cwd)
        finally:
            _sample()

    monkeypatch.setattr(submitter, "_run", _sampling_run)
    assert asyncio.run(submitter.submit(_submission())).reference == "note/agent-proposal"

    assert "merged-note" in seen_ids, "the reader saw nothing at all; the sampling is broken"
    assert "agent-proposal" not in seen_ids
    assert not any(file_seen)
    assert heads == {"ref: refs/heads/main"}
    # Non-vacuity: the submission really did happen, on its own branch, in its own worktree.
    assert "agent-proposal.md" in _git(
        knowledge_clone, "ls-tree", "-r", "--name-only", "note/agent-proposal"
    )


def test_the_shared_checkout_is_never_switched(knowledge_clone: Path) -> None:
    """One mechanical statement of the fix that no amount of restoring-in-`finally` can satisfy.

    A submitter that switched the tree and switched it back would pass every before/after
    assertion in this file. The reflog records the switch itself, so it distinguishes "restored"
    from "never left" — which is the difference the read window was made of.
    """
    asyncio.run(_submitter(knowledge_clone).submit(_submission()))

    reflog = _git(knowledge_clone, "reflog", "show", "HEAD")
    assert "note/" not in reflog, f"the shared checkout was switched: {reflog}"
    assert not list((knowledge_clone / ".git" / "chemclaw-worktrees").iterdir())


def test_a_leftover_worktree_from_a_crash_is_swept_and_the_note_can_be_reproposed(
    knowledge_clone: Path,
) -> None:
    """Crash recovery, built by hand so it is deterministic rather than timing-dependent.

    A SIGKILLed submission leaves both the worktree directory and its metadata, which is precisely
    the case `git worktree prune` does *not* handle — its default expiry is three months and it
    only reclaims metadata whose directory has vanished. So this asserts the two things that
    follow: the unreviewed note is invisible to a reader even while the leftover exists (it lives
    under `.git/`), and the retry of that same note succeeds instead of failing with "already used
    by worktree".
    """
    leftover = knowledge_clone / ".git" / "chemclaw-worktrees" / "note-agent-proposal"
    _git(knowledge_clone, "worktree", "add", "-B", "note/agent-proposal", str(leftover), "main")
    (leftover / "knowledge" / "reaction" / "agent-proposal.md").write_text(
        _UNREVIEWED, encoding="utf-8"
    )

    invalidate_cache(knowledge_clone / "knowledge")
    visible = {note.id for note in load_notes(knowledge_clone / "knowledge")}
    assert visible == {"merged-note"}, "a leftover worktree must not be visible to a reader"

    assert (
        asyncio.run(_submitter(knowledge_clone).submit(_submission())).reference
        == "note/agent-proposal"
    )
    assert not list((knowledge_clone / ".git" / "chemclaw-worktrees").iterdir())


def _push_succeeded(clone: Path, branch: str) -> bool:
    """Is `branch` actually on the remote with the note on it? The fact a caller must be told."""
    refs = _git(clone, "ls-remote", "--heads", "origin", branch)
    return branch in refs


@pytest.mark.parametrize(
    "failure", [OSError("git is gone"), asyncio.CancelledError()], ids=["error", "cancelled"]
)
def test_a_failing_worktree_cleanup_does_not_destroy_a_pushed_submission(
    knowledge_clone: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Cleanup runs after the push, so nothing it raises may replace the branch name.

    Measured before the fix: with the cleanup raising, the branch was on origin with the note's
    bytes on it while `submit` raised — `record_note` then recorded the proposal `failed`, the
    reviewer queue showed 0 and `close_merged_notes` moved 0. With `CancelledError`, a
    `BaseException` that `except Exception` does not catch, there was **no durable row at all**:
    a pushed, unreviewable, unrecorded note. Both cases are one defect — a `finally` that can raise
    — so both are pinned here.
    """
    submitter = _submitter(knowledge_clone)

    async def _boom(_workdir: Path) -> None:
        raise failure

    monkeypatch.setattr(submitter, "_remove_worktree", _boom)

    assert asyncio.run(submitter.submit(_submission())).reference == "note/agent-proposal"
    assert _push_succeeded(knowledge_clone, "note/agent-proposal")


def test_an_unremovable_worktree_is_left_for_the_next_sweep_not_lost(
    knowledge_clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost of swallowing: the scratch tree survives — and the next submission reclaims it.

    The pair to the test above. Swallowing the failure would be a leak if nothing else collected
    the directory, so this asserts the sweep that does.
    """
    submitter = _submitter(knowledge_clone)
    real = submitter._remove_worktree

    async def _boom(_workdir: Path) -> None:
        raise OSError("git is gone")

    monkeypatch.setattr(submitter, "_remove_worktree", _boom)
    asyncio.run(submitter.submit(_submission()))
    leftovers = knowledge_clone / ".git" / "chemclaw-worktrees"
    assert [p.name for p in leftovers.iterdir()] == ["note-agent-proposal"]

    monkeypatch.setattr(submitter, "_remove_worktree", real)
    asyncio.run(submitter.submit(_submission("other-note")))
    assert not list(leftovers.iterdir())


def test_a_checkout_parked_on_a_note_branch_by_an_older_version_is_repaired(
    knowledge_clone: Path,
) -> None:
    """The migration case, reproducing the measured backlog finding byte for byte.

    The old submitter restored the tree from a `finally`, which process death does not run — so a
    SIGKILL left `branch after SIGKILL: note/job-crash`, the unreviewed note present, and
    `load_notes` returning it as knowledge. Nothing in the new flow would ever move that tree back,
    and `worktree add -B` would fail for exactly the note whose submission crashed, so the repair
    is not optional: without it this change would entrench the finding it closes.
    """
    _git(knowledge_clone, "checkout", "-B", "note/agent-proposal")
    (knowledge_clone / "knowledge" / "reaction" / "agent-proposal.md").write_text(
        _UNREVIEWED, encoding="utf-8"
    )
    _git(knowledge_clone, "add", "-A")
    _git(knowledge_clone, "commit", "-m", "an interrupted submission")

    invalidate_cache(knowledge_clone / "knowledge")
    assert "agent-proposal" in {note.id for note in load_notes(knowledge_clone / "knowledge")}

    asyncio.run(_submitter(knowledge_clone).submit(_submission("other-note")))

    assert _git(knowledge_clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not (knowledge_clone / "knowledge" / "reaction" / "agent-proposal.md").exists()
    invalidate_cache(knowledge_clone / "knowledge")
    assert {note.id for note in load_notes(knowledge_clone / "knowledge")} == {"merged-note"}


def test_a_checkout_parked_on_an_operators_own_branch_is_left_alone(
    knowledge_clone: Path,
) -> None:
    """The negative control for the repair, which is otherwise a licence to switch someone's tree.

    Only a `note/` branch is a submission's residue. A clone deliberately left on any other branch
    belongs to whoever left it there.
    """
    _git(knowledge_clone, "checkout", "-B", "experiment")

    asyncio.run(_submitter(knowledge_clone).submit(_submission()))

    assert _git(knowledge_clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "experiment"


def test_the_note_type_records_who_authored_it(knowledge_clone: Path) -> None:
    """The mitigation that also exists, pinned so a change to it is deliberate.

    An agent-authored note carries `created_by: agent`, so a consumer can tell a proposal from
    merged knowledge if it looks. This asserts the field survives the round trip through the
    loader, which is the precondition for any downstream filter — and no reader applies one today,
    which the ADR says out loud rather than leaving the worktree fix to imply.
    """
    notes_dir = knowledge_clone / "knowledge"
    (notes_dir / "reaction" / "agent-proposal.md").write_text(_UNREVIEWED, encoding="utf-8")
    invalidate_cache(notes_dir)
    by_id = {note.id: note for note in load_notes(notes_dir)}
    assert by_id["agent-proposal"].created_by == "agent"
    assert by_id["merged-note"].created_by == "human"


def test_a_reader_is_not_excluded_while_the_submitter_holds_the_checkout(
    knowledge_clone: Path,
) -> None:
    """`load_notes` still takes no lock, and that is now harmless rather than the mechanism.

    The submitter holds two locks — a process-wide `asyncio.Lock` and an OS `flock`
    (`git_submitter._checkout_lock`) — and neither is a *reader* lock. That asymmetry used to be
    the whole exposure: nothing made a reader wait for the tree to settle. It is unchanged and now
    costs nothing, because there is no longer anything in the shared tree to exclude a reader from.

    Kept, rather than deleted as a test of an obsolete concern, because it is the pin that says no
    reader lock was ever added — if one is, this is where that decision surfaces.
    """
    notes_dir = knowledge_clone / "knowledge"
    invalidate_cache(notes_dir)

    with _checkout_lock(str(knowledge_clone)):
        during = load_notes(notes_dir)

    assert [note.id for note in during] == ["merged-note"]
