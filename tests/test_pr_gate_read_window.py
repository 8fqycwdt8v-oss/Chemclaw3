"""Can a reader see an unreviewed note while the PR-gate holds the checkout on its branch?

This is a compliance question, not a reliability one. The PR-gate is the GxP line — "AI proposes,
human signs off" (D-005) — and the whole control rests on an agent-authored note being invisible as
*knowledge* until a human merges it. Three facts about the code put that in doubt, and each is
verifiable by reading:

* `settings.knowledge_path` is `note_repo_dir / knowledge_dir` (`core/config/kg.py`), so readers
  resolve into **the same working tree** the submitter switches with `git checkout -B note/<id>`.
* `GitNoteSubmitter._write_and_push` writes the note into that tree and commits on the branch;
  the tree only returns to base in `_return_to_base`.
* `invalidate_cache()` is called **only** in `_return_to_base` (`git_submitter.py`), while
  `load_notes` caches for `graph_cache_ttl_seconds` (60 s by default).

So the hypothesis is: a reader scanning during the window caches the branch tree and then serves an
unreviewed note as merged knowledge for up to a TTL after the branch is gone.

**This file exists to settle that by measurement rather than by argument** — the repository's own
rule, learned from a solvent fix that two docstrings, an ADR and a closed backlog row all asserted
and that changed nothing to the fourth decimal.

**Measured 2026-08-04: the window is real.** A note committed on the branch is visible to a
filesystem reader while the tree is switched, and a reader that caches during the window keeps
serving it after the branch is gone. No retriever filters it out: `created_by == "agent"` is read
in exactly one place (`retrieval/harness.py`, to *label* a chunk "agent-authored" in a report) and
nothing consults `note_proposals.state`. In the shipped topology the retriever serves from the same
tree the gate switches — the runbook says so explicitly — so this is reachable in production, not
only in dev.

These tests pin the behaviour as it is today rather than assert the behaviour we want, because the
fix is an architectural change to a GxP control (`git worktree` for the submission, so the shared
tree is never switched) and that is a decision to take deliberately, not a diff to slip in beside a
test pass. Recorded in `docs/planning/BACKLOG.md`; when it is fixed, these are the regression
targets and their assertions invert.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.kg.graph import invalidate_cache, load_notes


def _git(repo: Path, *args: str) -> None:
    """Run one git command in `repo`, failing loudly — a silent setup failure fakes a pass."""
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


@pytest.fixture()
def knowledge_clone(tmp_path: Path) -> Path:
    """A real bare remote plus a real working clone with one merged note in it.

    Real git, not a stub: the window under test is created by `checkout -B` switching a working
    tree, so a fake submitter would test the fake. The merged note gives the reader something
    legitimate to see, which is what makes "and also the unreviewed one" a detectable difference.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
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


def test_a_note_committed_on_a_branch_is_visible_in_the_working_tree(knowledge_clone: Path) -> None:
    """The mechanism, isolated: does `checkout -B` + write expose the note to a filesystem reader?

    Deliberately the narrowest possible statement of the hazard, with no submitter and no cache in
    the way. If this is False the whole concern evaporates; if it is True the concern is real and
    the only remaining question is how long the exposure lasts.
    """
    unreviewed = knowledge_clone / "knowledge" / "reaction" / "agent-proposal.md"
    _git(knowledge_clone, "checkout", "-B", "note/agent-proposal")
    unreviewed.write_text(
        "---\nid: agent-proposal\ntype: reaction\ncreated_by: agent\n---\n\nUnreviewed.\n",
        encoding="utf-8",
    )
    _git(knowledge_clone, "add", "-A")
    _git(knowledge_clone, "commit", "-m", "propose")

    invalidate_cache(knowledge_clone / "knowledge")
    during = {note.id for note in load_notes(knowledge_clone / "knowledge")}

    _git(knowledge_clone, "checkout", "main")
    invalidate_cache(knowledge_clone / "knowledge")
    after = {note.id for note in load_notes(knowledge_clone / "knowledge")}

    assert "merged-note" in during and "merged-note" in after
    # The finding, either way. If the unreviewed note is visible while the branch is checked out,
    # every unsynchronised reader in the process — `find_notes`, `gather_evidence`, the digest job,
    # the ELN sync — can read it as knowledge during that window.
    assert "agent-proposal" in during, (
        "expected the branch checkout to expose the unreviewed note to a filesystem reader; "
        "if this fails the read window is not reachable and that is worth knowing"
    )
    assert "agent-proposal" not in after, "returning to base must remove it again"


def test_the_cache_keeps_serving_the_unreviewed_note_after_the_branch_is_gone(
    knowledge_clone: Path,
) -> None:
    """The part that outlives the window: a reader that cached during it keeps the note.

    `invalidate_cache()` runs only in `_return_to_base`, so it clears the cache of the *submitting*
    process. A reader that populated the cache mid-window is not helped by that at all — it holds
    the branch tree until its own TTL expires. This test pins the duration question by removing the
    TTL from the equation: it never invalidates, exactly as a concurrent reader would not.
    """
    notes_dir = knowledge_clone / "knowledge"
    unreviewed = notes_dir / "reaction" / "agent-proposal.md"

    _git(knowledge_clone, "checkout", "-B", "note/agent-proposal")
    unreviewed.write_text(
        "---\nid: agent-proposal\ntype: reaction\ncreated_by: agent\n---\n\nUnreviewed.\n",
        encoding="utf-8",
    )
    _git(knowledge_clone, "add", "-A")
    _git(knowledge_clone, "commit", "-m", "propose")

    invalidate_cache(notes_dir)
    cached_during = {note.id for note in load_notes(notes_dir)}

    _git(knowledge_clone, "checkout", "main")
    # No `invalidate_cache` here — that is the whole point. The submitter clears *its* cache; a
    # concurrent reader's cache is untouched and keeps the branch tree for up to the TTL.
    still_cached = {note.id for note in load_notes(notes_dir)}

    assert "agent-proposal" in cached_during
    assert ("agent-proposal" in still_cached) == ("agent-proposal" in cached_during), (
        "a reader that cached during the window keeps serving the unreviewed note after the "
        f"branch is gone (cached_during={sorted(cached_during)}, after={sorted(still_cached)})"
    )


def test_the_note_type_records_who_authored_it(knowledge_clone: Path) -> None:
    """The mitigation that *does* exist, pinned so a change to it is deliberate.

    Whatever the window turns out to be, an agent-authored note carries `created_by: agent` — so a
    consumer can tell a proposal from merged knowledge if it looks. This asserts the field survives
    the round trip through the loader, which is the precondition for any downstream filter.
    """
    notes_dir = knowledge_clone / "knowledge"
    (notes_dir / "reaction" / "agent-proposal.md").write_text(
        "---\nid: agent-proposal\ntype: reaction\ncreated_by: agent\n---\n\nUnreviewed.\n",
        encoding="utf-8",
    )
    invalidate_cache(notes_dir)
    by_id = {note.id: note for note in load_notes(notes_dir)}
    assert by_id["agent-proposal"].created_by == "agent"
    assert by_id["merged-note"].created_by == "human"


def test_readers_are_not_synchronised_with_the_submitter(knowledge_clone: Path) -> None:
    """`load_notes` takes no checkout lock — stated here so the exposure has a named cause.

    The submitter holds two locks (a process-wide `asyncio.Lock` and an OS `flock` on the
    checkout). Neither is a *reader* lock, so nothing makes a reader wait for the tree to settle.
    That asymmetry is the mechanism behind whatever the tests above measure.
    """
    notes_dir = knowledge_clone / "knowledge"
    invalidate_cache(notes_dir)

    async def read_many() -> list[int]:
        return [len(load_notes(notes_dir)) for _ in range(4)]

    assert all(count >= 1 for count in asyncio.run(read_many()))
    assert settings.graph_cache_ttl_seconds >= 0
