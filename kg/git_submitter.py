"""Git-backed NoteSubmitter: push an agent note on a feature branch (plan step 2.8).

The concrete `NoteSubmitter` for the PR-gate. It branches off the configured base,
writes the rendered note at its path, commits, and pushes the branch — the
reviewable unit a human then opens a PR for and merges (D-005). Opening the PR
object itself is the git platform's job (e.g. GitHub "create PR from branch"); this
submitter guarantees the agent note never lands directly on the base branch.

Concurrency: `git checkout -B` switches the *entire* working tree, so two
overlapping submissions would corrupt each other's branches. Submissions in this
process serialize through a module-level asyncio lock (per-note worktrees would
be over-engineering at current note volume — KISS). Cross-process ownership is
*enforced*, not just documented: each submission holds an exclusive OS-level
`flock` on a file under the checkout's `.git/`, so a second process pointing at
the same `note_repo_dir` gets a clear `GitSubmitError` instead of silently
interleaving checkouts. In production `settings.note_repo_dir` must still point
at a dedicated clone of the knowledge repo, never a tree with uncommitted work.
"""

import asyncio
import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path

from chemclaw.config import settings
from kg.pr_gate import NoteSubmission, NoteSubmitter

# Serializes every submit() in this process — see the module docstring.
_SUBMIT_LOCK = asyncio.Lock()

# The advisory-lock file guarding the checkout across processes. It lives under
# `.git/` so `reset --hard`/`clean -fd` (which submit() runs) can never delete it —
# unlinking a held lock file would let a second process lock a *new* inode at the
# same path and break mutual exclusion.
_LOCK_FILE_NAME = "chemclaw-submit.lock"


@contextlib.contextmanager
def _checkout_lock(repo_dir: str) -> Iterator[None]:
    """Hold an exclusive OS-level advisory lock on the checkout for one submission.

    The asyncio lock only serializes submissions *within* this process; two processes
    sharing `note_repo_dir` would still interleave `checkout -B` calls and corrupt
    each other's branches. A non-blocking exclusive `flock` turns that silent
    corruption into an immediate, actionable error. `flock` is tied to the open file
    description, so it genuinely excludes other processes and is released by the
    kernel even if this process dies mid-submission.

    Raises:
        GitSubmitError: When another process holds the lock, or the lock file cannot
            be opened (e.g. `repo_dir` is not a git checkout).
    """
    lock_path = Path(repo_dir) / ".git" / _LOCK_FILE_NAME
    try:
        lock_file = lock_path.open("a")
    except OSError as exc:
        raise GitSubmitError(f"cannot open submit lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitSubmitError(
                f"note_repo_dir is in use by another process (submit lock {lock_path} "
                "is held) — every process needs its own dedicated clone"
            ) from exc
        yield
    finally:
        lock_file.close()


class GitSubmitError(RuntimeError):
    """A git command in the submission flow failed."""


class GitNoteSubmitter:
    """Push a note on a per-note branch via git. Conforms to `NoteSubmitter`."""

    def __init__(
        self,
        repo_dir: str | None = None,
        base_branch: str | None = None,
        remote: str | None = None,
    ) -> None:
        """Configure the checkout, base branch, and remote (defaults from config)."""
        self._repo_dir = repo_dir if repo_dir is not None else settings.note_repo_dir
        self._base = base_branch if base_branch is not None else settings.note_base_branch
        self._remote = remote if remote is not None else settings.git_remote

    async def _run(self, *args: str) -> tuple[int, str]:
        """Run one git command in the repo; return (exit code, stderr) — no raise.

        Bounded by `git_command_timeout_seconds`: a hung command (dead remote,
        credential prompt) is killed and reported as a failure, so it can never
        deadlock the process-wide submit lock or orphan a git child holding
        `.git/index.lock`.
        """
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo_dir,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=settings.git_command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GitSubmitError(
                f"git {' '.join(args)} timed out after {settings.git_command_timeout_seconds}s"
            ) from exc
        except asyncio.CancelledError:
            # Kill the child so cancellation (e.g. Temporal activity timeout) never
            # orphans a git process, then let the cancellation propagate untouched.
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, stderr.decode().strip()

    async def _git(self, *args: str) -> None:
        """Run one git command, raising GitSubmitError on a non-zero exit."""
        returncode, stderr = await self._run(*args)
        if returncode != 0:
            raise GitSubmitError(f"git {' '.join(args)} failed: {stderr}")

    def _contained_note_path(self, relative: str) -> Path:
        """Resolve the note path and refuse anything escaping the checkout.

        Defense in depth behind the `Note` slug validation: even a hand-built
        `NoteSubmission` must not write outside the repo. Must be called *after*
        the branch checkout: `resolve()` follows symlinks in the working tree as
        it exists now, so checking the pre-checkout tree would let a symlinked
        directory committed on the base branch redirect the write.
        """
        repo_root = Path(self._repo_dir).resolve()
        note_path = (repo_root / relative).resolve()
        if not note_path.is_relative_to(repo_root):
            raise GitSubmitError(f"note path {relative!r} escapes the checkout {repo_root}")
        return note_path

    async def submit(self, submission: NoteSubmission) -> str:
        """Create the branch off the base, write+commit the note, and push it.

        Returns the pushed branch name — the reference a reviewer turns into a PR.
        If the note is byte-identical to what the base branch already contains,
        the branch name is returned *without* a push: there is nothing new to
        review, so no reviewable ref is (re)created.
        """
        async with _SUBMIT_LOCK:
            with _checkout_lock(self._repo_dir):
                return await self._submit_locked(submission)

    async def _submit_locked(self, submission: NoteSubmission) -> str:
        """The submission body, called with both the in-process and OS locks held."""
        await self._git("fetch", self._remote, self._base)
        # Start from a clean slate: a prior submission that died between `add`
        # and `commit` leaves its note staged, and `checkout -B` would carry
        # that residue into this note's branch and commit. Dropping staged,
        # dirty, and untracked state first also guarantees the checkout below
        # cannot fail on local changes. Safe because `note_repo_dir` is a
        # dedicated clone (module docstring) — there is never work to keep.
        await self._git("reset", "--hard")
        await self._git("clean", "-fd")
        await self._git("checkout", "-B", submission.branch, f"{self._remote}/{self._base}")
        note_path = self._contained_note_path(submission.path)

        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(submission.content, encoding="utf-8")

        await self._git("add", submission.path)
        # Idempotent: if the note is byte-identical to what the base already has,
        # there is nothing to commit — re-proposing it is a no-op, not an error.
        returncode, _ = await self._run("diff", "--cached", "--quiet")
        if returncode == 0:
            return submission.branch
        await self._git("commit", "-m", submission.title)
        # `--force-with-lease` needs a fresh remote-tracking ref: in a fresh
        # clone that never fetched the note branch, the lease is "stale" and
        # git rejects the push. Fetch it first, tolerating absence (first
        # submission of this note has no remote branch yet).
        await self._run(
            "fetch",
            self._remote,
            f"+refs/heads/{submission.branch}:refs/remotes/{self._remote}/{submission.branch}",
        )
        await self._git("push", "--force-with-lease", "-u", self._remote, submission.branch)
        return submission.branch


def default_submitter() -> NoteSubmitter:
    """The production note submitter (git feature branch). Overridden in tests."""
    return GitNoteSubmitter()
