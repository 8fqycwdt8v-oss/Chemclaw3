"""Git-backed `NoteWriter`: commit an agent note onto the base branch and push it.

The concrete writer behind `kg.record.record_note`. It writes each rendered note into the
checkout, stages exactly those paths, commits them as one unit and pushes the base branch, so a
note and the notes it cites reach a reader together or not at all. There is no review branch:
knowledge is written straight into the graph and corrected afterwards
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`).

Invariants:

- The checkout stays on the base branch and must be a dedicated clone, never the tree this
  process runs from nor a linked worktree (`_require_dedicated_checkout`).
- A write is all-or-nothing in the tree: every path is validated before any byte is written, each
  file is replaced atomically, and any failure restores the tree and un-stages the index.

Writes serialize in-process through a loop-local lock, across processes through an exclusive
`flock` under `.git/`, and across pods through a Postgres advisory lock.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from pathlib import Path

from chemclaw.core.aio import LoopLocalLock
from chemclaw.core.checkout import is_the_processes_own_checkout
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event, secret_env_names
from chemclaw.kg.graph import invalidate_cache, scan_notes_dir
from chemclaw.kg.note import NoteError, parse_note
from chemclaw.kg.record import NoteFile, NoteWrite, NoteWriter, WriteOutcome

log = logging.getLogger(__name__)

# Serializes writes on this event loop. Loop-local rather than a module-level `asyncio.Lock`,
# which binds to the first loop that contends on it and hangs a second `asyncio.run`; two
# simultaneous loops in separate threads are still excluded by the `flock` below.
_WRITE_LOCK = LoopLocalLock("kg.git_writer's write lock")

# Cross-process advisory-lock file. Under `.git/` so no reader's scan, `git clean` or the sidecar's
# `rsync --delete` touches it; `deploy/knowledge-sync.sh` hard-codes the same path, so the two must
# agree.
_LOCK_FILE_NAME = "chemclaw-submit.lock"

# Trailer on every commit this module makes, so `git log --grep` tells a recorded note from a
# hand-committed one, and the rebase knows which unpushed commits are ours to replay.
_RECORD_TRAILER = "Chemclaw-Note: recorded"


def _git_child_env() -> dict[str, str]:
    """This process's environment with its own secret values scrubbed, plus the commit identity.

    Remotes, credential helpers and hooks run with the child's environment, so this process's
    secrets (from `secret_env_names()`, the log-redaction inventory) are removed; the notes-remote
    token is not in that inventory and survives, which keeps `push` working. The commit identity is
    set via environment variables, which outrank any `user.*` config left in the clone; without it
    git in a container fails with `Author identity unknown`.
    """
    scrub = secret_env_names()
    env = {name: value for name, value in os.environ.items() if name not in scrub}
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = settings.note_committer_name
        env[f"GIT_{role}_EMAIL"] = settings.note_committer_email
    return env


def _replace_atomically(path: Path, content: bytes) -> None:
    """Put `content` at `path` in one step, so a concurrent reader never sees half of it.

    Readers hold no lock, and a truncated note can parse cleanly with half its wikilinks missing, so
    the write goes to a temporary file in the same directory and is moved with `os.replace`. Takes
    bytes so the rollback can restore any prior content, including non-UTF-8 notes. The target keeps
    its existing permissions; a new file gets what `open()` would give it, not the temp file's 0600.
    """
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, _mode_for(path))
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _mode_for(path: Path) -> int:
    """The permission bits a replacement of `path` should land with.

    The file's current mode, or 0666 masked by the umask for a new file. The umask can only be read
    by setting it; the value set in that window is the restrictive one.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0o077)
        os.umask(umask)
        return 0o666 & ~umask


def _git_dir(repo_dir: str) -> Path:
    """The checkout's `.git` directory, as a plain path computation.

    Not `git rev-parse`: `deploy/knowledge-sync.sh` hard-codes the same relative path so both take
    one lock, and tests build fake `.git` directories. Bare clones are refused elsewhere anyway.
    """
    return Path(repo_dir) / ".git"


@contextlib.contextmanager
def _checkout_lock(repo_dir: str) -> Iterator[None]:
    """Hold an exclusive OS-level advisory lock on the checkout for one write.

    Two processes sharing `note_repo_dir` share one working tree and index, so one would stage into
    the other's commit and its rollback would restore the other's bytes. A non-blocking `flock`
    turns that into an immediate error and is released by the kernel if this process dies.

    Raises:
        GitWriteError: When the lock file cannot be opened (e.g. `repo_dir` is not a checkout).
        GitRemoteError: When another process holds the lock.
    """
    lock_path = _git_dir(repo_dir) / _LOCK_FILE_NAME
    try:
        lock_file = lock_path.open("a")
    except OSError as exc:
        raise GitWriteError(f"cannot open submit lock {lock_path}: {exc}") from exc
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # Transient: the ordinary holder is the sync sidecar's publish tick or another
            # in-flight submission, both of which release within seconds — a retry succeeds.
            raise GitRemoteError(
                f"note_repo_dir is in use by another process (submit lock {lock_path} "
                "is held); retrying after the holder releases it"
            ) from exc
        yield
    finally:
        lock_file.close()


class GitWriteError(ChemclawError):
    """The note write failed in a way a retry cannot fix.

    A `ChemclawError`, so the reason reaches the model instead of a generic tool failure. Registered
    non-retryable (`durable.publish._BAD_DATA_TYPES`), so it is raised only for structural failures:
    a mis-pointed checkout, a path escaping the tree, a denied credential. Transient failures raise
    `GitRemoteError`.
    """


class GitRemoteError(GitWriteError):
    """A transient failure: the remote, the network, or a lock another process holds.

    A subclass so `except GitWriteError` still catches it, with a different name because Temporal's
    `non_retryable_error_types` matches by class name and this one must stay retryable.
    """


def _require_dedicated_checkout(repo_dir: str) -> None:
    """Refuse a checkout that is verifiably the process's own working tree.

    A write commits into the tree it is handed and pushes its base branch to `origin`; pointed at
    the running application's checkout (and `note_repo_dir` defaults to `"."`) it would commit into
    the source tree and push to the source repository. A linked worktree is refused too, because the
    write lock's path under `.git/` is a pointer file there.

    Raises:
        GitWriteError: When `repo_dir` resolves to the process CWD, to the root of the git
            checkout the process is running from, or to a linked worktree.
    """
    resolved = Path(repo_dir).resolve()
    # Shared with `core/netguard.py`, which omits this tree's remote from the egress allowlist
    # because the write is refused here; both must ask the same question.
    if is_the_processes_own_checkout(repo_dir):
        raise GitWriteError(
            f"note_repo_dir {repo_dir!r} resolves to {resolved} — the checkout this "
            "process is running from. A note write commits into that tree and pushes it to its "
            "origin, which would publish agent-authored notes into the source repository. "
            "Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone of the knowledge repo."
        )
    if (resolved / ".git").is_file():
        raise GitWriteError(
            f"note_repo_dir {repo_dir!r} is a linked git worktree; a note write needs a clone "
            "with a real .git directory. Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone."
        )


# Phrases git or a forge prints when the remote refused this credential rather than being
# unreachable; matched lower-cased. Covers GitHub/OpenSSH, GitLab, Bitbucket and Azure DevOps.
# Bare status codes are excluded on purpose: `403` is also a rate limit, and classifying a
# throttle as auth would drop the note permanently. A missed phrase errs safe (retried as
# transient); a false positive does not.
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "invalid username or password",
    "could not read username",
    "could not read password",
    "permission denied",
    "access denied",
    "support for password authentication was removed",
    # GitHub: a token that exists and works, for an organisation that will not accept it until it
    # is SSO-authorized. Retrying installs no authorization.
    "enabled or enforced saml sso",
    # GitLab: "You are not allowed to push code to this project." / "... to upload code." / "... to
    # force push code to a protected branch".
    "you are not allowed to",
    # Bitbucket Server / Data Center.
    "you are not permitted to",
    # Bitbucket Cloud.
    "write access to repository not granted",
    # Azure DevOps: the Git permission refusal, by its stable error id rather than by the prose
    # around it, which names whichever permission is missing.
    "tf401027",
    # Gerrit: a ref-permission refusal from the server's own access control.
    "prohibited by gerrit",
)


# A forge naming the principal it refused (`Permission to owner/repo.git denied to user.`); a
# pattern because the repository sits between the words.
_DENIED_PRINCIPAL = re.compile(r"permission to .{0,200}? denied to ", re.IGNORECASE | re.DOTALL)


# Cap on git text in one log record. Stderr carries remote hook output and the arguments carry a
# branch name, neither ours; `SecretRedactingFilter` regex-scans each message under the logging
# lock, so an unbounded field stalls every logging thread. The raised exception keeps the full text.
_LOGGED_TEXT_LIMIT = 2000


def _for_log(text: str, limit: int = _LOGGED_TEXT_LIMIT) -> str:
    """`text` bounded for one log record, saying so when it was cut rather than cutting silently."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more character(s) omitted]"


# What git prints when it cannot take the index lock: the one local failure a retry can clear.
_INDEX_LOCK_MARKERS = ("index.lock", "file exists")


def _is_a_contended_index(stderr: str) -> bool:
    """Whether git refused because `.git/index.lock` already exists.

    The one local failure that is retryable: the holder is a running git child or a stale file a
    killed one left, which `_clear_a_stale_index_lock` removes on the next attempt. Non-retryable,
    it would drop every note on the pod until a human deleted the file.
    """
    lowered = stderr.lower()
    return all(marker in lowered for marker in _INDEX_LOCK_MARKERS)


def _is_auth_failure(stderr: str) -> bool:
    """Whether git's stderr says the remote refused this credential.

    A credential failure is a fact about the token, so it is raised non-retryable rather than
    retried forever. A bare status code is not enough (a `403` may be a rate limit); a genuine
    denial always says so in words.
    """
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return True
    return _DENIED_PRINCIPAL.search(stderr) is not None


class GitNoteWriter:
    """Commit a note onto the base branch via git. Conforms to `NoteSubmitter`."""

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

    async def _exec(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        """Spawn one git child and collect it under a bound; return (exit code, stdout, stderr).

        The only place a git process starts, so its bounds hold for every call: a command exceeding
        `git_command_timeout_seconds`, or whose caller is cancelled, is killed rather than left
        running or holding the write lock. The kill is `SIGKILL` and may leave `.git/index.lock`
        behind; that failure is classified retryable and the stale file is cleared on the next
        write.
        """
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo_dir,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_child_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=settings.git_command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GitRemoteError(
                f"git {' '.join(argv)} timed out after {settings.git_command_timeout_seconds}s"
            ) from exc
        except asyncio.CancelledError:
            # Kill the child so cancellation (e.g. Temporal activity timeout) never
            # orphans a git process, then let the cancellation propagate untouched.
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()

    async def _run(self, *args: str) -> tuple[int, str]:
        """Run one git command in the repo; return (exit code, stderr) without raising."""
        returncode, _stdout, stderr = await self._exec(args)
        return returncode, stderr

    async def _git(self, *args: str, transient: bool = False) -> None:
        """Run one git command, raising on a non-zero exit, and log what git said.

        `transient=True` marks fetch and push, whose ordinary failure is the network's, so they
        raise the retryable `GitRemoteError`. Local operations raise the non-retryable class, except
        a contended index lock, which is retryable anywhere. An authentication failure is
        non-retryable even when transient, since no retry installs a token. Git's stderr is logged
        here because callers may drop it; both logged strings are capped (`_for_log`), and URL
        credentials are stripped by the redacting filter.
        """
        returncode, stderr = await self._run(*args)
        if returncode == 0:
            return
        auth = transient and _is_auth_failure(stderr)
        # A contended index is the one *local* failure a retry clears — see `_is_a_contended_index`.
        retryable = (transient and not auth) or _is_a_contended_index(stderr)
        error = GitRemoteError if retryable else GitWriteError
        log_event(
            log,
            "git.failed",
            "git %s failed (%d)%s: %s",
            _for_log(" ".join(args)),
            returncode,
            " — an authentication failure, which no retry can fix" if auth else "",
            _for_log(stderr),
            level=logging.WARNING,
            command=args[0],
            returncode=returncode,
            retryable=retryable,
        )
        raise error(f"git {' '.join(args)} failed: {stderr}")

    async def _read(self, *args: str) -> str | None:
        """One git query's stdout, stripped, or `None` on a non-zero exit."""
        # A single argument is a ref to resolve; a full argv is run verbatim.
        argv = args if len(args) > 1 else ("rev-parse", "--verify", "--quiet", args[0])
        returncode, stdout, _stderr = await self._exec(argv)
        # A non-zero exit is an answer here ("no such ref"), not an error.
        return None if returncode != 0 else stdout

    def _contained_note_path(self, relative: str) -> Path:
        """Resolve the note path inside the notes checkout and refuse anything escaping it.

        Defence in depth behind the `Note` slug validation. `resolve()` follows symlinks on disk, so
        a committed symlinked directory cannot redirect the write. Paths under `.git/` are refused
        too: they resolve inside the checkout, and `.git/config` or `.git/hooks/*` would give
        control over the push target or code execution (and the write lock lives there).
        """
        root = Path(self._repo_dir).resolve()
        note_path = (root / relative).resolve()
        if not note_path.is_relative_to(root):
            raise GitWriteError(f"note path {relative!r} escapes the checkout {root}")
        if ".git" in note_path.relative_to(root).parts:
            raise GitWriteError(
                f"note path {relative!r} reaches into the git directory of {root}; a note is a "
                "file in the knowledge tree, never repository metadata"
            )
        return note_path

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Write the note's files into the checkout, commit them and push.

        Returns the commit that landed; `notes=0` when every file was byte-identical to the tree, in
        which case nothing is committed. A `repo_dir` that is the process's own checkout is refused
        up front.
        """
        _require_dedicated_checkout(self._repo_dir)
        async with _WRITE_LOCK:
            async with self._cluster_lock():
                with _checkout_lock(self._repo_dir):
                    return await self._write_locked(write)

    @contextlib.asynccontextmanager
    async def _cluster_lock(self) -> AsyncIterator[None]:
        """Serialize writes to one remote across pods, via a Postgres advisory lock.

        Each pod's clone is local, so the `flock` cannot exclude another pod. With
        `session_store="postgres"`, a write takes a session-level advisory lock keyed on the remote
        URL for its duration; `pg_advisory_lock` queues, bounded by the statement timeout, and a
        timeout raises the retryable `GitRemoteError`. A memory-store deployment is single-process,
        and the chart refuses to render with any other session store. A race that slips through only
        diverges the clones, which `_replay_our_unpushed_commits` resolves on the next write.
        """
        if settings.session_store != "postgres":
            yield
            return
        from chemclaw.core import db

        returncode, url = await self._run("config", "--get", f"remote.{self._remote}.url")
        identity = url if returncode == 0 and url else f"{self._repo_dir}:{self._remote}"
        key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        # Only acquisition failures are wrapped; wrapping the `yield` would relabel a write error as
        # a lock error. The operation is named because this connection is held across the whole
        # fetch, commit and push, which would otherwise read as slow database queries.
        connection_ctx = db.connection(settings.postgres_dsn, operation="kg_cluster_submit_lock")
        try:
            conn = await connection_ctx.__aenter__()
        except Exception as exc:
            raise GitRemoteError(
                f"could not reach Postgres for the cluster submit lock: {exc}"
            ) from exc
        try:
            try:
                await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            except Exception as exc:
                raise GitRemoteError(f"could not take the cluster submit lock: {exc}") from exc
            yield
        finally:
            # Best effort: the lock is session-scoped, so closing the connection releases it even
            # when the explicit unlock cannot run.
            with contextlib.suppress(Exception):
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            with contextlib.suppress(Exception):
                await connection_ctx.__aexit__(None, None, None)

    async def _write_locked(self, write: NoteWrite) -> WriteOutcome:
        """The write body, called with the in-process, cluster and OS locks held.

        The checkout is the tree readers scan, so it fast-forwards first: committing on a stale base
        would hide what another pod recorded. If `--ff-only` fails, this clone holds commits the
        remote lacks (a failed push leaves the note committed locally), and those are rebased onto
        the remote, but only when every one carries `_RECORD_TRAILER`; a person's local commit is
        never moved.

        A hard kill leaves residue no handler cleared: a stale `.git/index.lock` is removed before
        the index is touched, and staged-but-uncommitted blobs are discarded only after the
        fast-forward has failed, since only then is the residue known to block the checkout.
        """
        branch = await self._read("symbolic-ref", "--short", "HEAD")
        if branch != self._base:
            raise GitWriteError(
                f"the notes checkout at {self._repo_dir!r} is on {branch!r}, not the base branch "
                f"{self._base!r}. Recorded notes are committed to the base branch, and readers "
                "scan this same tree, so a checkout parked elsewhere would serve the wrong notes."
            )
        self._clear_a_stale_index_lock()
        await self._git("fetch", self._remote, self._base, transient=True)
        returncode, stderr = await self._run("merge", "--ff-only", f"{self._remote}/{self._base}")
        if returncode != 0 and await self._discard_a_dead_writes_residue():
            returncode, stderr = await self._run(
                "merge", "--ff-only", f"{self._remote}/{self._base}"
            )
        if returncode != 0:
            await self._replay_our_unpushed_commits(stderr)
        return await self._write_and_commit(write)

    def _clear_a_stale_index_lock(self) -> None:
        """Remove a `.git/index.lock` a killed git child left behind, with the flock held.

        A `SIGKILL`ed child or an evicted pod leaves the lock file, and every later `git add` fails.
        The flock proves no peer writer is mid-write; the age bound (older than
        `git_command_timeout_seconds`) covers a person running git by hand, who takes no flock. A
        younger lock raises the retryable `GitRemoteError` so a later attempt finds it stale.
        Best-effort: a lost race fails retryably.
        """
        lock_path = _git_dir(self._repo_dir) / "index.lock"
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return
        if age <= settings.git_command_timeout_seconds:
            return
        with contextlib.suppress(OSError):
            lock_path.unlink()
            log_event(
                log,
                "kg.write.stale_index_lock_cleared",
                "removed a stale %s left by a killed git process (%.0fs old)",
                lock_path,
                age,
                level=logging.WARNING,
                age_seconds=round(age),
            )

    async def _discard_a_dead_writes_residue(self) -> bool:
        """Put the knowledge tree back to `HEAD` when a dead write's residue is blocking the merge.

        Returns whether anything was discarded, so the caller can fast-forward again.

        A `SIGKILL` between `git add` and the commit leaves the note's blobs staged and its bytes in
        the tree; once another pod pushes the same paths, `--ff-only` refuses and nothing else
        recovers the pod. Called only after the fast-forward has failed, because a staged note is
        indistinguishable from an operator's staged work; at that point the alternative is the pod
        never recording again. Logged at WARNING with the paths. Scoped to `knowledge_dir`, every
        path this writer can stage.
        """
        # `-z`: git quotes a path with unusual bytes in the default listing, and a quoted path is
        # not the pathspec the reset below needs.
        listing = await self._read(
            "diff", "--cached", "--name-only", "-z", "HEAD", "--", settings.knowledge_dir
        )
        orphaned = [path for path in (listing or "").split("\0") if path]
        if not orphaned:
            return False
        log_event(
            log,
            "kg.write.dead_write_residue_discarded",
            "the fast-forward is blocked by %d path(s) a killed write left staged in %s; "
            "restoring them to HEAD so this pod can record again: %s",
            len(orphaned),
            self._repo_dir,
            _for_log(", ".join(orphaned)),
            level=logging.WARNING,
            paths=len(orphaned),
        )
        # Un-staging alone is not enough: untracked residue blocks `merge --ff-only` too, so the
        # worktree is restored as well.
        await self._git("reset", "-q", "HEAD", "--", *orphaned)
        # Paths `HEAD` holds are restored with `checkout --`; the rest existed only in the dead
        # write and are removed.
        tracked_listing = await self._read("ls-files", "-z", "--", *orphaned)
        tracked = [path for path in (tracked_listing or "").split("\0") if path]
        if tracked:
            await self._git("checkout", "-q", "--", *tracked)
        for path in set(orphaned) - set(tracked):
            with contextlib.suppress(OSError):
                self._contained_note_path(path).unlink(missing_ok=True)
        # These bytes were in the tree `load_notes` scans, so a warm reader is holding them.
        invalidate_cache()
        return True

    async def _replay_our_unpushed_commits(self, why: str) -> None:
        """Rebase this clone's own unpushed note commits onto the remote, or refuse.

        Every local commit must carry `_RECORD_TRAILER` (stranded by a failed push); anything else
        is a person's commit and is not this writer's to rewrite. Zero local commits is reported
        separately, since then the fast-forward failed on a dirty tree or index, not on commits.
        """
        subjects = await self._read("log", "--format=%B%x00", f"{self._remote}/{self._base}..HEAD")
        ours = [
            message
            for message in (subjects or "").split("\0")
            if message.strip() and _RECORD_TRAILER in message
        ]
        total = [message for message in (subjects or "").split("\0") if message.strip()]
        if not total:
            raise GitRemoteError(
                f"the notes checkout could not fast-forward onto {self._remote}/{self._base} and "
                "holds no local commits to replay, so the checkout itself is what refuses — a "
                f"modified file or a staged change in {self._repo_dir!r}: {why}"
            )
        if len(ours) != len(total):
            raise GitRemoteError(
                f"the notes checkout could not fast-forward onto {self._remote}/{self._base} and "
                f"holds {len(total) - len(ours)} local commit(s) this system did not write, so "
                f"nothing here may move them: {why}"
            )
        # `--rebase-merges` is deliberately absent: every commit being replayed is one of ours and
        # linear by construction — this module only ever fast-forwards or rebases.
        returncode, stderr = await self._run("rebase", f"{self._remote}/{self._base}")
        if returncode != 0:
            # Leave no half-finished rebase behind for the next write to trip over.
            await self._run("rebase", "--abort")
            raise GitRemoteError(
                f"could not replay {len(ours)} unpushed note commit(s) onto "
                f"{self._remote}/{self._base}: {stderr}"
            )
        log_event(
            log,
            "kg.write.replayed",
            "replayed %d unpushed note commit(s) onto %s/%s",
            len(ours),
            self._remote,
            self._base,
            commits=len(ours),
            base=self._base,
        )

    async def _write_and_commit(self, write: NoteWrite) -> WriteOutcome:
        """Write the files in order, commit, and push the base branch.

        The caller's order is load-bearing: `record._build_write` puts dependencies before the
        subject and retirements after it, so a reader scanning mid-write never sees a note before
        what it cites. Each path is containment-checked independently. The graph cache is
        invalidated because these bytes land in the tree readers scan.
        """
        # Every path is resolved and checked before any byte is written.
        planned: list[tuple[Path, NoteFile]] = []
        # One tree scan for the whole write rather than one per file; the scan dominates the cost.
        contained = [self._contained_note_path(file.path) for file in write.files]
        curated_by_id = self._persons_notes_claiming(contained)
        for note_path, file in zip(contained, write.files, strict=True):
            if not file.overwrite and note_path.exists():
                continue
            # An amendment to a person's note is dropped, not refused; the subject note keeps the
            # hard refusal. See `_refuse_to_clobber_a_person`.
            curated = curated_by_id.get(note_path.stem)
            if file.amendment and curated is not None:
                log_event(
                    log,
                    "kg.write.amendment_left_alone",
                    "left %s alone: it is a human's note, so it is not retired in place — the "
                    "note recorded alongside it still lands and marks it as contradicted",
                    curated,
                    level=logging.WARNING,
                    path=file.path,
                    curated_path=str(curated),
                )
                continue
            self._refuse_to_clobber_a_person(curated, file.path)
            planned.append((note_path, file))
        if not planned:
            return WriteOutcome(reference=self._base, notes=0)

        # What each target held before this write, so a failure before the commit can put the tree
        # back. `None` means the file did not exist.
        prior = {path: (path.read_bytes() if path.exists() else None) for path, _ in planned}
        written = [file.path for _, file in planned]
        # Count of subject notes this write changes, computed here because only this frame has both
        # the prior bytes and the per-file flags; dependencies and retirements are not counted.
        subjects = _changed_subjects(planned, prior)
        committed = False
        try:
            for note_path, file in planned:
                note_path.parent.mkdir(parents=True, exist_ok=True)
                _replace_atomically(note_path, file.content.encode("utf-8"))
            # `--` ends option parsing: a leading-dash path resolves inside the repo but would reach
            # git as an option.
            await self._git("add", "--", *written)
            # Idempotent and scoped to our paths: byte-identical content stages nothing. Without the
            # path scope, something else already staged would send a no-op write into the
            # path-limited commit below, which then fails non-retryably.
            returncode, _ = await self._run("diff", "--cached", "--quiet", "HEAD", "--", *written)
            committed = returncode != 0
            if committed:
                # Path-limited so anything else left staged in the shared index is not committed
                # under this note.
                await self._git(
                    "commit", "-m", write.message, "-m", _RECORD_TRAILER, "--", *written
                )
        except BaseException:
            # The bytes are in the tree readers scan, so restore every target and re-raise. Each
            # restore is attempted independently and a failure is logged with the path to repair by
            # hand, so one failing restore cannot skip the rest or the un-stage; the original
            # exception propagates.
            for note_path, content in prior.items():
                try:
                    if content is None:
                        note_path.unlink(missing_ok=True)
                    else:
                        _replace_atomically(note_path, content)
                except Exception:
                    log.exception(
                        "kg.write.rollback_failed: could not restore %s — it may still hold the "
                        "bytes of a write that did not land, and needs a manual "
                        "`git checkout -- <path>` in %s",
                        note_path,
                        self._repo_dir,
                    )
            # Un-stage our paths too: a failure after `git add` leaves the retracted blob in the
            # index, where any unscoped commit would publish it and a later fast-forward touching
            # those paths would refuse. A kill that skips this handler is recovered by
            # `_discard_a_dead_writes_residue`.
            with contextlib.suppress(ChemclawError):
                await self._run("reset", "-q", "HEAD", "--", *written)
            invalidate_cache()
            raise

        # Invalidated before the push and unconditionally: the bytes are already in the scanned
        # tree, even if the push then fails.
        invalidate_cache()
        commit = await self._read("rev-parse", "HEAD")
        return await self._push(
            commit,
            subjects=len(subjects),
            planned_subjects=_subject_count(planned),
            committed=committed,
        )

    async def _push(
        self,
        commit: str | None,
        *,
        subjects: int = 1,
        planned_subjects: int = 1,
        committed: bool = True,
    ) -> WriteOutcome:
        """Push the base branch, and report `written` by what the remote now has.

        Reached from the no-diff path too: a retry after a failed push stages nothing but must still
        push the earlier commit, so `written` depends on the local base being ahead of its
        remote-tracking ref, not on whether this call staged anything.
        """
        ahead = await self._read("rev-list", "--count", f"{self._remote}/{self._base}..HEAD")
        if ahead == "0":
            return WriteOutcome(reference=commit or self._base, notes=0)
        # Where this call committed, the count is the changed subjects, floored at 1 so a commit
        # that changed only a dependency or retirement still reports `written`. Where it only pushes
        # an earlier attempt's commit, nothing changed now, so the count is the subjects this write
        # names; a batch that was partly pushed before may over-count, which is the safe direction.
        landed = max(subjects, 1) if committed else planned_subjects
        try:
            # Through `_git` so a push rejection reaches the credential classifier and stderr is
            # logged.
            await self._git("push", self._remote, f"HEAD:refs/heads/{self._base}", transient=True)
        except GitWriteError as exc:
            # Keep the class `_git` chose, and say that the note is already committed locally and
            # readable: only the push failed, and the next attempt pushes this same commit. Without
            # that the model retries with permuted arguments.
            raise type(exc)(
                f"{exc} — the note is committed on {self._base} in this checkout and is already "
                f"readable here; only the push to {self._remote} did not happen, so re-record "
                "nothing and change nothing: the next attempt pushes this same commit."
            ) from exc
        return WriteOutcome(reference=commit or self._base, notes=landed)

    def _is_a_persons_note(self, note_path: Path) -> bool:
        """Whether a human's note is already at `note_path`; an unparseable or absent file is not one.
        """
        if not note_path.exists():
            return False
        try:
            existing = parse_note(note_path)
        except (NoteError, OSError):
            return False
        return existing.created_by == "human"

    def _persons_notes_claiming(self, targets: Sequence[Path]) -> dict[str, Path]:
        """For each id a write is about to take, the human-authored note already holding it.

        A note's identity is its id, not its path: the graph keeps the first file in path order for
        a duplicated id, so an agent note at `campaign/<id>.md` would shadow a curated
        `playbook/<id>.md` without touching it. So ids are looked up over the same path-ordered scan
        the graph uses (`graph.scan_notes_dir`), with the file stem as the id (`kg.validate`
        enforces they agree). Ties go to the first in path order, the file the graph serves. One
        scan serves every target, and only matching stems are parsed.
        """
        wanted = {path.stem for path in targets}
        found: dict[str, Path] = {}
        for root in sorted({path.parent.parent for path in targets}):
            if not root.is_dir():
                continue
            for candidate, _ in scan_notes_dir(root):
                if candidate.stem not in wanted or candidate.stem in found:
                    continue
                if self._is_a_persons_note(candidate):
                    found[candidate.stem] = candidate
        return found

    def _refuse_to_clobber_a_person(self, curated: Path | None, relative: str) -> None:
        """Refuse to overwrite a note a human authored.

        `record_note` checks `created_by` on the note it is given, not on what already holds that
        id, so this is the check that keeps an agent write from replacing curated knowledge.
        Disagreement with a curated note is expressed by a new note with a `contradicts` edge, which
        only works while the curated note is still served. Only the subject is refused: an amendment
        (retirement) of a person's note steps aside in `_write_and_commit`, so the new note still
        lands and the curated one stays open. `curated` is `_persons_notes_claiming`'s answer, and
        the message names that file when it differs from the one being written.
        """
        if curated is None:
            return
        held = self._relative_to_checkout(curated)
        elsewhere = (
            ""
            if held == relative
            else f" — note id {curated.stem!r} is already held by {held!r}, which the graph serves "
            "in preference to this path"
        )
        raise GitWriteError(
            f"refusing to write {relative!r}{elsewhere}: it is authored by a human, and an agent "
            "write may not replace, retire or take the id of curated knowledge. Record a new note "
            "that contradicts or supersedes it instead."
        )

    def _relative_to_checkout(self, path: Path) -> str:
        """`path` relative to the checkout, for a message a person can paste into `git`.

        Avoids leaking the pod's mount point; a path outside the checkout is kept whole.
        """
        try:
            return path.relative_to(Path(self._repo_dir).resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def _in_sequential_order(files: Iterable[NoteFile]) -> list[NoteFile]:
    """Drop the batch files a sequential application would have skipped, keeping order.

    `GitNoteWriter` evaluates `overwrite=False` against the tree before writing anything, so a batch
    cannot see its own earlier files. Here a path written earlier in the batch counts as existing,
    so a later do-not-clobber copy of it is dropped. Paths already in the tree are left to the inner
    writer.
    """
    kept: list[NoteFile] = []
    written: set[str] = set()
    for file in files:
        if not file.overwrite and file.path in written:
            continue
        kept.append(file)
        written.add(file.path)
    return kept


class BatchingNoteWriter:
    """A `NoteWriter` that lands many notes in one commit, for a backfill only.

    One commit and one push per note bounds a backfill, so this merges N `NoteWrite`s into a single
    write and hands it to the wrapped `GitNoteWriter`; it adds no git code. Not for the
    conversational path: a queued note is one a chemist cannot read yet, and `record_note`'s
    reference for a pending note is the empty string. A caller must `flush()`.

    Files are concatenated with `overwrite=False` resolved against the batch as well as the tree
    (`_in_sequential_order`), so the result matches applying the writes one by one.

    `write` reports `notes=0` and the flush carries the count, so `chemclaw_notes_recorded_total`
    counts notes that reached git, never accepted-but-unwritten ones.
    """

    def __init__(self, inner: NoteWriter, batch_size: int) -> None:
        """Wrap `inner`, committing every `batch_size` notes. Below 2 is a bug, not a mode."""
        if batch_size < 2:
            raise ValueError(
                f"batch_size {batch_size} does not batch anything; use the wrapped writer directly"
            )
        self._inner = inner
        self._batch_size = batch_size
        self._pending: list[NoteWrite] = []

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Hold this note; commit the batch when it is full. Returns an empty pending reference."""
        self._pending.append(write)
        if len(self._pending) >= self._batch_size:
            return await self.flush()
        # Nothing has reached the graph yet; the flush that commits the batch counts its notes.
        return WriteOutcome(reference="", notes=0)

    async def flush(self) -> WriteOutcome:
        """Commit and push everything held, as one write. A no-op when nothing is pending."""
        if not self._pending:
            return WriteOutcome(reference="", notes=0)
        batch, self._pending = self._pending, []
        files = _in_sequential_order(file for write in batch for file in write.files)
        # The subject of a batch names the count rather than the notes: `NoteWrite` refuses a
        # message over its own length bound, and fifty note ids do not fit in one.
        outcome = await self._inner.write(
            NoteWrite(files=files, message=f"Add {len(batch)} backfilled note(s)")
        )
        # The inner write already counts the changed subjects, so the outcome passes through.
        return outcome


def _changed_subjects(
    planned: list[tuple[Path, NoteFile]], prior: dict[Path, bytes | None]
) -> list[str]:
    """The distinct subject notes this write genuinely changes: the honest `notes` count.

    `record._build_write` tags dependencies `overwrite=False` and retirements `amendment=True`, so
    subjects are the remaining files. Distinct, because a batch may name one subject twice.

    Args:
        planned: The `(absolute path, file)` pairs this write will put in the tree.
        prior: What each path held before, `None` if absent; the same bytes the rollback restores.

    Returns:
        The repo-relative paths of the subject notes whose bytes this write changes.
    """
    changed = {
        file.path
        for note_path, file in planned
        if file.overwrite
        and not file.amendment
        and prior.get(note_path) != file.content.encode("utf-8")
    }
    return sorted(changed)


def _subject_count(planned: list[tuple[Path, NoteFile]]) -> int:
    """How many distinct subject notes this write names, changed or not.

    Used by `_push` when landing an earlier attempt's commit, where nothing changed in this call.
    """
    return len({file.path for _, file in planned if file.overwrite and not file.amendment})


def default_writer() -> NoteWriter:
    """The production note writer: a commit on the notes repo's base branch. Overridden in tests."""
    return GitNoteWriter()
