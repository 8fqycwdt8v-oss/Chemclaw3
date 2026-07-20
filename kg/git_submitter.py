"""Git-backed NoteSubmitter: push an agent note on a feature branch (plan step 2.8).

The concrete `NoteSubmitter` for the PR-gate. It branches off the configured base,
writes the rendered note at its path, commits, and pushes the branch — the
reviewable unit a human then opens a PR for and merges (D-005). Opening the PR
object itself is the git platform's job (e.g. GitHub "create PR from branch"); this
submitter guarantees the agent note never lands directly on the base branch.

It mutates a git working tree, so point it at a dedicated checkout, never a tree
with uncommitted work.
"""

import asyncio
from pathlib import Path

from chemclaw.config import settings
from kg.pr_gate import NoteSubmission


class GitSubmitError(RuntimeError):
    """A git command in the submission flow failed."""


class GitNoteSubmitter:
    """Push a note on a per-note branch via git. Conforms to `NoteSubmitter`."""

    def __init__(
        self,
        repo_dir: str = ".",
        base_branch: str | None = None,
        remote: str | None = None,
    ) -> None:
        """Configure the checkout, base branch, and remote (defaults from config)."""
        self._repo_dir = repo_dir
        self._base = base_branch if base_branch is not None else settings.note_base_branch
        self._remote = remote if remote is not None else settings.git_remote

    async def _git(self, *args: str) -> None:
        """Run one git command in the repo, raising GitSubmitError on failure."""
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo_dir,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitSubmitError(f"git {' '.join(args)} failed: {stderr.decode().strip()}")

    async def submit(self, submission: NoteSubmission) -> str:
        """Create the branch off the base, write+commit the note, and push it.

        Returns the pushed branch name — the reference a reviewer turns into a PR.
        """
        await self._git("fetch", self._remote, self._base)
        await self._git("checkout", "-B", submission.branch, f"{self._remote}/{self._base}")

        note_path = Path(self._repo_dir) / submission.path
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(submission.content, encoding="utf-8")

        await self._git("add", submission.path)
        await self._git("commit", "-m", submission.title)
        await self._git("push", "--force-with-lease", "-u", self._remote, submission.branch)
        return submission.branch
