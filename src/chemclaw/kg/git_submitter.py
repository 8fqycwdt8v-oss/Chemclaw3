"""Git-backed NoteSubmitter: push an agent note on a feature branch (plan step 2.8).

The concrete `NoteSubmitter` for the PR-gate. It branches off the configured base,
writes the rendered note at its path, commits, and pushes the branch — the
reviewable unit a human then opens a PR for and merges (D-005). Opening the PR
object itself is the git platform's job (e.g. GitHub "create PR from branch"); this
submitter guarantees the agent note never lands directly on the base branch.

**The submission happens in its own `git worktree`, and the shared checkout is never switched.**
That is a review property, not a performance one. `note_repo_dir` is also what every reader resolves
through `settings.knowledge_path`, so while a submission held that tree on `note/<id>` the
unreviewed note was readable *as knowledge* by `find_notes`, `gather_evidence`, the digest job and
the ELN sync — and, because `load_notes` caches, for up to `graph_cache_ttl_seconds` after the
branch was gone. A crash was worse: `_return_to_base` ran from a `finally`, which SIGKILL does not,
so the tree stayed parked on the note branch indefinitely. Both were measured, both are closed by
the same change, and neither could be closed by restoring the tree more carefully — the exposure
existed for as long as the tree was switched at all.

An earlier version of this docstring rejected per-note worktrees as over-engineering *for
concurrency*. That conclusion still stands: worktrees buy isolation from readers, not throughput,
and concurrency is unchanged — one submission at a time per `note_repo_dir`, one submitting process
per host. They are adopted here for a reason that was never weighed.

Concurrency, then, as before: submissions in this process serialize through a module-level asyncio
lock, and cross-process ownership is *enforced* by an exclusive OS-level `flock` on a file under
the checkout's `.git/`. Both are still load-bearing. `git worktree add` mutates `.git/worktrees/`
and the ref store, and the leftover sweep below removes *every* worktree under our own root — which
is only safe because no other submission can be in flight. In production `settings.note_repo_dir`
must still point at a dedicated clone of the knowledge repo.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import re
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import degraded
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.submission import NoteSubmission, NoteSubmitter, SubmissionOutcome

log = logging.getLogger(__name__)

# Serializes every submit() in this process — see the module docstring.
_SUBMIT_LOCK = asyncio.Lock()

# The advisory-lock file guarding the checkout across processes. It lives under `.git/` because
# nothing else writes there: no reader's `rglob` reaches it (`knowledge_path` is
# `note_repo_dir/knowledge_dir`), `git clean -fd` never touches it, and the deployment sidecar's
# `rsync --delete` publishes only into the knowledge directory. `deploy/knowledge-sync.sh`
# hard-codes this same relative path — the two are the same lock or they are no lock at all.
_LOCK_FILE_NAME = "chemclaw-submit.lock"

# The trailer every gate commit carries, and the guard that reads it. A proposal branch is the
# gate's channel: replacing it wholesale is what a re-proposal *is*, and is safe exactly while
# every commit on it is the gate's own. The moment a human pushes to `note/<id>` — a reviewer's
# fixup — a force-push would discard work nobody chose to discard, so `_write_and_push` refuses
# when the remote tip does not carry this trailer. (`--force-with-lease` could never make that
# distinction: the lease is refreshed by the fetch each submission starts with, so it only guards
# the fetch-to-push window, not the reviewer's commit that was already there.)
_GATE_TRAILER = "Chemclaw-PR-Gate: submission"

# Where a submission's private worktree lives, beside the lock file and for the same reason: it is
# the one location inside the repository that no reader and no sync can see. A module constant
# rather than a setting — a knob here would cost an `.env.example` row, a Helm ConfigMap entry and
# two pinning tests to let an operator move a directory nothing outside this file knows about.
_WORKTREE_DIR_NAME = "chemclaw-worktrees"


def _git_dir(repo_dir: str) -> Path:
    """The checkout's `.git` directory — a plain path computation, deliberately.

    Not `git rev-parse --git-common-dir`: `tests/test_concurrency_claims.py` builds fake `.git`
    directories that are not repositories to prove the cross-process lock is real without a
    remote, and `deploy/knowledge-sync.sh` hard-codes the same relative path so the sidecar and the
    submitter take the same lock. Both would break for the sake of supporting a bare clone, which
    `_require_dedicated_checkout` refuses anyway.
    """
    return Path(repo_dir) / ".git"


@contextlib.contextmanager
def _checkout_lock(repo_dir: str) -> Iterator[None]:
    """Hold an exclusive OS-level advisory lock on the checkout for one submission.

    The asyncio lock only serializes submissions *within* this process. Two processes sharing
    `note_repo_dir` would still both mutate `.git/worktrees/` and the ref store, and — the part
    that makes this lock structural rather than cautious — the leftover sweep each submission runs
    removes every worktree under our root, which is correct exactly because no other submission can
    own one. Without this lock the sweep would delete a sibling process's live worktree mid-push.

    A non-blocking exclusive `flock` turns that into an immediate, actionable error. `flock` is
    tied to the open file description, so it genuinely excludes other processes and is released by
    the kernel even if this process dies mid-submission.

    Raises:
        GitSubmitError: When another process holds the lock, or the lock file cannot
            be opened (e.g. `repo_dir` is not a git checkout).
    """
    lock_path = _git_dir(repo_dir) / _LOCK_FILE_NAME
    try:
        lock_file = lock_path.open("a")
    except OSError as exc:
        raise GitSubmitError(f"cannot open submit lock {lock_path}: {exc}") from exc
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


class GitSubmitError(ChemclawError):
    """The submission flow refused or failed in a way a retry cannot fix.

    A `ChemclawError`, so `agent.tool_authz.surface_domain_errors` shows the reason to the model.
    As a bare `RuntimeError` it did not, and the 2026-08-02 live run measured what that costs:
    every `propose_knowledge_note` call failed, the model was told only "Error: Function failed.",
    it retried five times permuting its *arguments* because nothing said the problem was elsewhere,
    and then printed the ungated document into the chat as a fallback. The PR-gate's failure mode
    was to publish without the gate.

    **This name is registered non-retryable** (`durable.publish._BAD_DATA_TYPES`), so it is
    raised only for the failures where that is true: a mis-pointed checkout, a path escaping the
    tree, a proposal branch carrying commits this gate did not author. A dead remote, a timed-out
    command or a contended lock is `GitRemoteError` below — the split this class used to not
    have, which made `note_write_max_attempts` dead for exactly the failures it was configured
    for: a 30-second network blip dropped a note from a synthesis batch on the first attempt
    while three docstrings said it would be retried.
    """


class GitRemoteError(GitSubmitError):
    """A transient failure — the remote, the network, or a lock another process holds.

    A *subclass*, so every `except GitSubmitError` caller still catches it; a *different name*,
    because Temporal's `non_retryable_error_types` matches by class name and the whole point is
    that this one is not on the list. `note_publish_retry()`'s bound is what limits the retries.
    """


def _process_repo_root() -> Path | None:
    """The root of the git checkout this process runs from, or None outside any checkout.

    The nearest ancestor of the CWD containing `.git` — the tree whose uncommitted work
    would be destroyed if the submitter's reset/clean ever ran against it.
    """
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _require_dedicated_checkout(repo_dir: str) -> None:
    """Refuse a checkout that is verifiably the process's own working tree (G4).

    The reason is not the one this guard was written for. It used to be that every submission
    began `git reset --hard` + `git clean -fd`, which would wipe uncommitted work in the service's
    own repo; the submission no longer touches the shared tree at all, so that reason is gone.

    What remains is worse. A submission creates `note/<id>` in this repository and
    **`git push --force-with-lease` to its `origin`** — so pointed at the ChemClaw source checkout,
    the gate would publish an agent-authored knowledge note into the code repository. That is not
    hypothetical: `note_repo_dir` still defaults to `"."`.

    Also refuses a `repo_dir` whose `.git` is a *file* rather than a directory, i.e. a linked
    worktree. Both the submit lock and the worktree root are plain paths under `<repo>/.git/`, and
    inside a linked worktree that is a pointer file — which surfaces later as a confusing failure
    rather than here as a clear one.

    Raises:
        GitSubmitError: When `repo_dir` resolves to the process CWD, to the root of the git
            checkout the process is running from, or to a linked worktree.
    """
    resolved = Path(repo_dir).resolve()
    if resolved == Path.cwd().resolve() or resolved == _process_repo_root():
        raise GitSubmitError(
            f"note_repo_dir {repo_dir!r} resolves to {resolved} — the checkout this "
            "process is running from. Submissions create and force-push note branches to its "
            "origin, which would publish agent-authored notes into the source repository. "
            "Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone of the knowledge repo."
        )
    if (resolved / ".git").is_file():
        raise GitSubmitError(
            f"note_repo_dir {repo_dir!r} is a linked git worktree; the PR-gate needs a clone with "
            "a real .git directory. Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone."
        )


# What git says when the remote refused *us* rather than being unreachable. Phrases rather than
# bare status codes: `403` as a substring matches an object hash, and the point of this list is to
# be wrong in the safe direction — a missed phrase keeps today's behaviour (retried as transient),
# while a false positive would make a genuine network blip permanent. Lower-cased before matching.
#
# **The bare status lines are gone, and that is a correction rather than a trim.** `403` is what a
# forge returns for a *secondary rate limit* as well as for a denial: GitHub answers
# `fatal: unable to access '...': The requested URL returned error: 403` for abuse detection and
# push throttling, both of which clear on their own in seconds to minutes. Classified as auth, that
# raises `GitSubmitError`, which `durable/publish.py` lists as non-retryable — so a throttle would
# permanently drop the note proposal instead of backing off, and the PR-gate would quietly stop
# proposing while every run reported success. The comment above claims this list is "wrong in the
# safe direction", and those two entries were the only ones that were not.
#
# Nothing is lost by removing them: a genuine denial from a forge carries a phrase as well as a
# status — GitHub's is `remote: Permission to owner/repo.git denied to user.`, matched by
# `permission denied` — so the credential cases below still classify, and a bare status line with
# no accompanying phrase stays transient, which is the safe direction for a code that has two
# meanings.
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "invalid username or password",
    "could not read username",
    "could not read password",
    "permission denied",
    "access denied",
    "support for password authentication was removed",
)


# A forge naming the principal it refused. Matched as a pattern rather than a substring because the
# real wording puts the repository between the two words — GitHub says
# `remote: Permission to owner/repo.git denied to some-bot.`, which the substring
# `permission denied` does not match at all. That gap is why the bare status lines looked load-
# bearing: they were catching this case by accident, and catching a rate limit with it.
_DENIED_PRINCIPAL = re.compile(r"permission to .{0,200}? denied to ", re.IGNORECASE | re.DOTALL)


def _is_auth_failure(stderr: str) -> bool:
    """Whether git's stderr says the remote refused this credential.

    The distinction `durable/publish.py` cannot make and this can: a *credential* failure on a push
    is a fact about the token, and retrying it forever is how an expired PAT becomes an
    indefinitely retrying workflow whose log says only that a publish failed.

    **A bare status code is deliberately not enough.** `403` is what a forge returns for a
    secondary rate limit as well as for a denial, and `GitSubmitError` is non-retryable — so
    classifying a throttle as auth drops the note proposal instead of backing off, and the PR-gate
    stops proposing while every run still reports success. A genuine denial always says so in
    words as well, either with one of the credential phrases or by naming the principal it
    refused, so nothing is lost by requiring the words.
    """
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return True
    return _DENIED_PRINCIPAL.search(stderr) is not None


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

    async def _run(self, *args: str, cwd: str | None = None) -> tuple[int, str]:
        """Run one git command in the repo (or in `cwd`); return (exit code, stderr) — no raise.

        Bounded by `git_command_timeout_seconds`: a hung command (dead remote,
        credential prompt) is killed and reported as a failure, so it can never
        deadlock the process-wide submit lock or orphan a git child holding
        `.git/index.lock`.

        `cwd` is how the write half of a submission runs inside its worktree. **Every** git
        command goes through here, including the worktree ones: the timeout and the kill-on-cancel
        are properties of this function, and `tests/test_knowledge.py` fakes
        `create_subprocess_exec` to prove them, so a command issued any other way would be
        unbounded and invisible at once.
        """
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd if cwd is not None else self._repo_dir,
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
            raise GitRemoteError(
                f"git {' '.join(args)} timed out after {settings.git_command_timeout_seconds}s"
            ) from exc
        except asyncio.CancelledError:
            # Kill the child so cancellation (e.g. Temporal activity timeout) never
            # orphans a git process, then let the cancellation propagate untouched.
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, stderr.decode().strip()

    async def _git(self, *args: str, cwd: str | None = None, transient: bool = False) -> None:
        """Run one git command, raising on a non-zero exit — and log what git actually said.

        `transient=True` marks the commands whose ordinary failure is the *network's* — fetch and
        push — so they raise the retryable `GitRemoteError`. Local operations (worktree, add,
        commit, checkout) fail for structural reasons a retry replays identically, and keep the
        non-retryable class.

        **An expired credential is not a network partition, and this used to classify it as one.**
        `transient=True` covers fetch and push, so a 403 from the git host raised `GitRemoteError`
        exactly like a dropped connection — and `durable/publish.py` catches that, logs the note's
        label and *drops the message*, so the distinguishing text never reached a log at all while
        the job retried indefinitely against a credential that will never work again. Two changes,
        both needed: git's own stderr is logged **here**, at the raise, where it still exists; and
        an authentication failure is raised non-retryable, because no number of retries installs a
        token.

        The stderr is git's output rather than a user's text, and `SecretRedactingFilter` strips
        URL userinfo from every record, so a remote carrying `user:token@` before its host cannot
        put its credential into this line. (Written without the scheme deliberately:
        `tests/test_no_egress.py` reads every `http(s)://` literal in first-party source as a host
        this system dials, and an illustrative one in prose is indistinguishable from a real one.)
        """
        returncode, stderr = await self._run(*args, cwd=cwd)
        if returncode == 0:
            return
        auth = transient and _is_auth_failure(stderr)
        error = GitRemoteError if transient and not auth else GitSubmitError
        log_event(
            log,
            "git.failed",
            "git %s failed (%d)%s: %s",
            " ".join(args),
            returncode,
            " — an authentication failure, which no retry can fix" if auth else "",
            stderr,
            level=logging.WARNING,
            command=args[0],
            returncode=returncode,
            retryable=error is GitRemoteError,
        )
        raise error(f"git {' '.join(args)} failed: {stderr}")

    async def _read(self, *args: str) -> str | None:
        """One git query's stdout, stripped — or `None` on a non-zero exit.

        `_run` returns stderr because the write path only ever needs the error text; the tip
        guard needs *answers* (a hash, a commit message), and reading them off stderr is how its
        first draft compared two empty strings and concluded there was nothing to lose.
        """
        # A single argument is a ref to resolve; a full argv is run verbatim.
        argv = args if len(args) > 1 else ("rev-parse", "--verify", "--quiet", args[0])
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo_dir,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=settings.git_command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GitRemoteError(f"git {' '.join(argv)} timed out") from exc
        if process.returncode != 0:
            return None
        return stdout.decode().strip()

    def _contained_note_path(self, relative: str, workdir: Path) -> Path:
        """Resolve the note path inside `workdir` and refuse anything escaping it.

        Defense in depth behind the `Note` slug validation: even a hand-built
        `NoteSubmission` must not write outside the tree. Must be called *after*
        the worktree exists: `resolve()` follows symlinks in the working tree as
        it is materialized, so checking an unmaterialized tree would let a symlinked
        directory committed on the base branch redirect the write.

        This is also the reason the worktree is created *with* a checkout rather than with
        `--no-checkout` + `read-tree`, which would make a submission cost one file write instead of
        materializing the corpus: with nothing on disk there is no symlink to resolve and this
        check silently passes. Worth revisiting only if the corpus grows large enough for the
        per-submission checkout to show up in submission latency — and then with a different
        containment check, not with this one weakened.
        """
        root = workdir.resolve()
        note_path = (root / relative).resolve()
        if not note_path.is_relative_to(root):
            raise GitSubmitError(f"note path {relative!r} escapes the checkout {root}")
        return note_path

    def _worktree_root(self) -> Path:
        """Where this submitter's private worktrees live: `<repo>/.git/chemclaw-worktrees/`."""
        return _git_dir(self._repo_dir) / _WORKTREE_DIR_NAME

    def _workdir_for(self, branch: str) -> Path:
        """The worktree directory for `branch` — named after it, not after a random id.

        Deterministic so an operator running `git worktree list` after an incident sees
        `note-job-crash` and knows what it is and where it came from. Uniqueness comes from the
        two locks and the sweep, not from the name.
        """
        return self._worktree_root() / branch.replace("/", "-")

    async def submit(self, submission: NoteSubmission) -> SubmissionOutcome:
        """Create the branch off the base, write+commit the note, and push it.

        Returns the pushed branch name — the reference a reviewer turns into a PR — with
        `pushed=False` when the note is byte-identical to what the base branch already contains:
        there is nothing new to review, so no reviewable ref is (re)created. A `repo_dir` that is
        the process's own checkout is refused up front (`_require_dedicated_checkout`)
        — submissions force-push note branches to its origin and need a dedicated clone.
        """
        _require_dedicated_checkout(self._repo_dir)
        async with _SUBMIT_LOCK:
            async with self._cluster_lock():
                with _checkout_lock(self._repo_dir):
                    return await self._submit_locked(submission)

    @contextlib.asynccontextmanager
    async def _cluster_lock(self) -> AsyncIterator[None]:
        """Serialize submissions to one remote across pods, via a Postgres advisory lock.

        The `flock` below is host-local: each pod's clone lives in its own `emptyDir`, so N pods
        held N independent locks against one origin, and two pods proposing the same note id
        concurrently were last-writer-wins with no error. The database every durable deployment
        already shares is the one mutual ground, so where it is configured
        (`session_store="postgres"`), submissions take a session-level advisory lock keyed on the
        remote URL for the duration of the submit. `pg_advisory_lock` *queues* rather than fails,
        which is the right shape — a waiting pod waits exactly as long as the submission it would
        otherwise have raced. The wait is bounded by the connection's statement timeout, and a
        timeout raises the retryable `GitRemoteError`.

        A memory-store deployment (the CLI, tests) is single-process by construction and skips it.
        """
        if settings.session_store != "postgres":
            yield
            return
        from chemclaw.core import db

        returncode, url = await self._run("config", "--get", f"remote.{self._remote}.url")
        identity = url if returncode == 0 and url else f"{self._repo_dir}:{self._remote}"
        key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        # The acquisition failures are wrapped; the submission body's own exceptions are not —
        # a broad handler around the `yield` would relabel a genuine submit error as a lock one.
        connection_ctx = db.connection(settings.postgres_dsn)
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

    async def _submit_locked(self, submission: NoteSubmission) -> SubmissionOutcome:
        """The submission body, called with both the in-process and OS locks held.

        The note branch is checked out into a worktree under `.git/`, which no reader is ever
        pointed at, so the shared working tree is not switched, not written to, and not restored —
        there is nothing to restore. That is the whole of the fix for the read window and for the
        crash that used to leave the tree parked on `note/<id>`: an exposure that lasts as long as
        the tree is switched cannot be closed by switching it back more reliably.

        The two steps before the worktree is created are the ones that make this safe to deploy
        into a repository that has already been through the old behaviour.
        """
        await self._git("fetch", self._remote, self._base, transient=True)
        # The note branch too, *here* — before anything is written — so the remote-tracking ref
        # the push's `--force-with-lease` reads is the state of the world this submission started
        # from. It used to be fetched one line above the push, which set the lease to "whatever
        # is on the remote right now" and made it a plain `--force` wearing the safe flag's name.
        # Absence is tolerated: the first submission of a note has no remote branch yet.
        await self._run(
            "fetch",
            self._remote,
            f"+refs/heads/{submission.branch}:refs/remotes/{self._remote}/{submission.branch}",
        )
        await self._require_gate_authored_tip(submission.branch)
        await self._repair_parked_checkout()
        await self._sweep_leftover_worktrees()
        workdir = self._workdir_for(submission.branch)
        await self._git(
            "worktree",
            "add",
            "-B",
            submission.branch,
            str(workdir),
            f"{self._remote}/{self._base}",
        )
        try:
            return await self._write_and_push(submission, workdir)
        finally:
            await self._release_worktree(workdir)

    async def _require_gate_authored_tip(self, branch: str) -> None:
        """Refuse to replace a proposal branch whose tip this gate did not mint.

        A re-proposal force-pushes `note/<id>`, which is safe exactly while every commit there is
        the gate's own — and every gate commit carries `_GATE_TRAILER` for this check to read. A
        tip without it means a human pushed to the proposal branch (a reviewer's fixup is the
        ordinary case), and discarding their commit silently inside a PR titled as a proposal is
        the one thing this flow must never do. Non-retryable on purpose: the identical retry
        would meet the identical foreign commit; resolving it is a decision on the branch, not a
        second attempt.

        A branch that does not exist on the remote, or whose tip is the base tip, passes — there
        is nothing there to lose.
        """
        ref = f"refs/remotes/{self._remote}/{branch}"
        branch_tip = await self._read(ref)
        if branch_tip is None:
            return
        base_tip = await self._read(f"refs/remotes/{self._remote}/{self._base}")
        if base_tip is not None and branch_tip == base_tip:
            return
        # `%(trailers…)` would be cleaner, but the message body is what the gate writes and what
        # survives hosts that rewrite committer identity; a substring over one commit is enough.
        message = await self._read("log", "-1", "--format=%B", ref)
        if message is None or _GATE_TRAILER not in message:
            raise GitSubmitError(
                f"the remote branch {branch!r} carries a commit this gate did not author — "
                "someone pushed to the proposal branch, and replacing it would discard their "
                "work. Resolve the branch in the git host (merge or delete it), then re-propose."
            )

    async def _repair_parked_checkout(self) -> None:
        """Move the shared tree off a `note/` branch a previous version left it on.

        Migration, and it must exist or this change *entrenches* the finding it closes: with
        `_return_to_base` gone nothing else would ever move that tree back, and `worktree add -B`
        would then fail with "already used by worktree" for exactly the note whose submission
        crashed — the retry most likely to happen next.

        `checkout -f`, not `reset --hard` + `clean -fd`: it restores tracked files (the unreviewed
        note is tracked on the note branch, so it goes) without deleting untracked ones, which in
        the shipped topology are the notes the sync sidecar publishes into this clone and which may
        not exist in its base commit. Restricted to the `note/` prefix so an operator's own branch
        is never touched, and it reads `.git/HEAD` directly — one line, the same file
        `git symbolic-ref` reads — so the check costs nothing in the steady state where it can
        never fire.
        """
        try:
            head = (_git_dir(self._repo_dir) / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            # Swallowed because a repair cannot be a precondition for a submission — but *said*,
            # because this is the branch on which the repair silently does not happen: the next
            # `worktree add -B` then fails with "already used by worktree", which names neither
            # this file nor this function and reads as a git bug rather than an unreadable HEAD.
            degraded(
                log,
                "note_repo",
                "cannot read %s/HEAD; skipping the parked-worktree repair. A submission "
                "interrupted on a note/ branch will fail with 'already used by worktree'",
                self._repo_dir,
                level=logging.WARNING,
            )
            return
        if not head.startswith("ref: refs/heads/note/"):
            return
        branch = head.removeprefix("ref: refs/heads/")
        log.warning(
            "note repo %s was left checked out on %s by an interrupted submission; "
            "returning it to %s",
            self._repo_dir,
            branch,
            self._base,
        )
        try:
            await self._git("checkout", "-f", self._base)
        finally:
            # The shared tree changed either way — back to base, or to whatever a failed switch
            # left — and a cached graph describing the note branch must not outlive it under
            # `graph_cache_ttl_seconds`. This is the one path in the submitter that still touches
            # what readers read, and therefore the one that still busts their cache.
            invalidate_cache()

    async def _sweep_leftover_worktrees(self) -> None:
        """Remove any worktree a previous submission left behind, before creating this one's.

        `git worktree prune` alone does **not** do this, and assuming it does is the easy mistake:
        prune removes metadata whose *directory has vanished*, on a three-month default expiry, so
        against a SIGKILLed submission — which leaves both the directory and the metadata — it is a
        no-op. The directory sweep is what actually reclaims those; the prune afterwards covers the
        inverse case, a directory removed out from under git.

        Runs at the start of every submit rather than once at startup, for two reasons: `-B` fails
        while a leftover still holds `note/<id>`, so the retry of a crashed note stays wedged until
        this runs; and `default_submitter()` builds a submitter per call, so "startup" names no
        moment. The cost on the happy path is one `listdir`.

        Safe because it only ever touches children of this submitter's own root — never the main
        worktree, never an operator's — and because both locks are held, so no concurrent
        submission can own one of them. That clause is why the flock stays repo-wide.
        """
        root = self._worktree_root()
        if root.is_dir():
            for leftover in sorted(root.iterdir()):
                log.warning("removing leftover submission worktree %s", leftover)
                await self._remove_worktree(leftover)
        await self._run("worktree", "prune", "--expire", "now")

    async def _release_worktree(self, workdir: Path) -> None:
        """Dispose of a finished submission's worktree; **never** at the cost of its result.

        This is the `finally` of `_submit_locked`, and it runs *after* the branch is on the remote.
        Anything that escapes it replaces the pushed branch name with an exception, which is a lie
        about what happened to the repository — and a consequential one: `propose_note` then records
        the proposal `failed`, so the reviewer queue shows nothing to review while the branch is on
        origin, and `close_merged_notes` never moves the row. Under `CancelledError` — a
        `BaseException`, so `except Exception` around the caller does not see it — there was no
        durable row at all: a pushed note nothing anywhere knows about.

        So every failure is swallowed here, including cancellation. The obligation is genuinely one
        sided: the branch is the product of a submission and it already exists, while an unremoved
        scratch tree costs disk under `.git/` (where no reader looks) until the next submission's
        sweep reclaims it. Cancellation is swallowed rather than re-raised for the same reason — a
        caller that must record what was pushed cannot be told the call did not finish. Whoever
        cancelled gets the return value of an operation that had already succeeded.

        **`BaseException` means every one of them, and two consequences follow that are worth
        naming rather than discovering.** An operator's Ctrl-C that lands inside this window is
        logged as a warning and goes no further — the process finishes the submission it was in the
        middle of recording and exits on the *next* one. And a task cancelled at this instant goes
        on to run `record_proposal_submitted`: that is a single bounded database write with the
        connection's statement timeout on it, so a cancelled task cannot hang here, but it does do
        one more thing after being cancelled. Both are the intended price of the branch never being
        recorded as `failed`; neither is a way for shutdown to become unbounded.
        """
        try:
            await self._remove_worktree(workdir)
        except BaseException as exc:
            log.warning(
                "could not remove submission worktree %s (%s); leaving it for the "
                "next submission's sweep",
                workdir,
                exc,
            )

    async def _remove_worktree(self, workdir: Path) -> None:
        """Remove one worktree: `git worktree remove`, falling back to deleting the directory.

        A different obligation from the `_return_to_base` it replaces: that had to *restore* a
        shared tree and a failure to do so was unrecoverable, while this disposes of a scratch tree
        whose only cost, if it survives, is disk — and the next submission's sweep reclaims it. The
        unreviewed bytes it holds sit under `.git/`, where no reader looks.

        A non-zero git is handled here; anything *raised* is not, and reaches the
        caller. That is right for `_sweep_leftover_worktrees`, which runs before anything is pushed
        and where an unremovable leftover should fail the submission loudly rather than let
        `worktree add -B` fail confusingly later. The post-push caller wraps this in
        `_release_worktree` instead, because there the same raise would destroy a result.

        Never deletes the branch: the branch is the reviewable unit and the whole product of a
        submission.
        """
        returncode, stderr = await self._run("worktree", "remove", "--force", str(workdir))
        if returncode == 0:
            return
        log.warning("git worktree remove %s failed (%s); removing the directory", workdir, stderr)
        shutil.rmtree(workdir, ignore_errors=True)
        await self._run("worktree", "prune", "--expire", "now")

    async def _write_and_push(self, submission: NoteSubmission, workdir: Path) -> SubmissionOutcome:
        """Write the submission's files into its worktree, commit, and push the branch.

        Every git command here runs with `-C <workdir>`. Objects, remote-tracking refs and config
        live in the common `.git` directory, so fetch and push behave from a linked worktree
        exactly as they did from the main one.

        **Deliberately does not bust the graph cache**, and now for a plain reason rather than a
        subtle one: nothing a reader can see has changed. The note exists only on `note/<id>` and
        in a directory under `.git/` that no reader scans. Busting would advertise a tree change
        that did not happen and pay an O(notes) rescan for it. Post-merge freshness never came from
        here — the sidecar's `rsync` is a different process — and reaches readers through the stat
        fingerprint within `graph_cache_ttl_seconds` (DA-5).
        """
        work = str(workdir)
        # Every file in the submission, not just the subject note: a note and the notes its links
        # depend on land together or the links dangle (STO-7, see `NoteSubmission`). Each path is
        # containment-checked independently — a dependency is no more trusted than the note.
        # A file marked `overwrite=False` (a machine-rendered dependency) is written only when the
        # base branch has none: `NoteFile` says why an unconditional write silently reverted a
        # human's post-merge edits.
        written: list[str] = []
        for file in submission.files:
            note_path = self._contained_note_path(file.path, workdir)
            if not file.overwrite and note_path.exists():
                continue
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(file.content, encoding="utf-8")
            written.append(file.path)
        if not written:
            return SubmissionOutcome(reference=submission.branch, pushed=False)
        # `--` ends option parsing before the note paths: `_contained_note_path` only checks
        # containment, and a leading-dash relative path (e.g. `-x`) resolves *inside* the worktree
        # and would otherwise reach git as an option rather than a pathspec.
        await self._git("add", "--", *written, cwd=work)
        # Idempotent: if the note is byte-identical to what the base already has,
        # there is nothing to commit — re-proposing it is a no-op, not an error.
        returncode, _ = await self._run("diff", "--cached", "--quiet", cwd=work)
        if returncode == 0:
            return SubmissionOutcome(reference=submission.branch, pushed=False)
        # The trailer is what `_require_gate_authored_tip` reads on the next re-proposal to tell
        # this gate's own tip from a human's commit on the branch.
        await self._git("commit", "-m", submission.title, "-m", _GATE_TRAILER, cwd=work)
        # The lease was fetched at the start of `_submit_locked`, before anything was written, so
        # it protects the whole read-decide-push window: a push that lands on the remote between
        # our fetch and this line fails the lease instead of being clobbered.
        returncode, stderr = await self._run(
            "push", "--force-with-lease", "-u", self._remote, submission.branch, cwd=work
        )
        if returncode != 0:
            if "stale info" in stderr or "[rejected]" in stderr:
                # The remote moved during the submission window. Transient in the mechanical
                # sense — but a blind retry would fetch the mover's commit as its new lease and
                # overwrite it, so this goes through the *tip guard* instead: the retryable error
                # here re-runs the submission from the top, where `_require_gate_authored_tip`
                # decides whether what landed is the gate's own (safe to replace) or a human's
                # (refused with instructions).
                raise GitRemoteError(
                    f"the remote moved while submitting {submission.branch!r}: {stderr}"
                )
            raise GitRemoteError(f"git push {submission.branch} failed: {stderr}")
        return SubmissionOutcome(reference=submission.branch)


def default_submitter() -> NoteSubmitter:
    """The production note submitter (git feature branch). Overridden in tests."""
    return GitNoteSubmitter()
