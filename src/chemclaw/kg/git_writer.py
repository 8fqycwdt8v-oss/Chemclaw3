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
import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event, secret_env_names
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.record import NoteWrite, NoteWriter, WriteOutcome

log = logging.getLogger(__name__)

# Serializes every submit() in this process — see the module docstring.
_WRITE_LOCK = asyncio.Lock()

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
_RECORD_TRAILER = "Chemclaw-Note: recorded"

# Where a submission's private worktree lives, beside the lock file and for the same reason: it is
# the one location inside the repository that no reader and no sync can see. A module constant
# rather than a setting — a knob here would cost an `.env.example` row, a Helm ConfigMap entry and
# two pinning tests to let an operator move a directory nothing outside this file knows about.
_WORKTREE_DIR_NAME = "chemclaw-worktrees"


def _git_child_env() -> dict[str, str]:
    """This process's environment with its own secret values scrubbed, for a git child.

    Least privilege: git needs `PATH`, `HOME`, `SSH_*`, `GIT_*`, any proxy and the notes-remote
    credential — all of which stay — but never this process's LLM key, database DSNs, Temporal key
    or the framing-envelope HMAC. A configured git remote, a credential helper or a `git` hook runs
    with the child's environment, so leaving those there would hand them to code this process does
    not control. The scrubbed names come from `secret_env_names()`, which reads the same inventory
    the log redaction does, so the set cannot drift from it; the notes-remote token is not in that
    inventory and so survives, which is what keeps `push` working.
    """
    scrub = secret_env_names()
    return {name: value for name, value in os.environ.items() if name not in scrub}


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
        GitWriteError: When another process holds the lock, or the lock file cannot
            be opened (e.g. `repo_dir` is not a git checkout).
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
    """The submission flow refused or failed in a way a retry cannot fix.

    A `ChemclawError`, so `agent.tool_authz.surface_domain_errors` shows the reason to the model.
    As a bare `RuntimeError` it did not, and the 2026-08-02 live run measured what that costs:
    every `record_knowledge_note` call failed, the model was told only "Error: Function failed.",
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


class GitRemoteError(GitWriteError):
    """A transient failure — the remote, the network, or a lock another process holds.

    A *subclass*, so every `except GitWriteError` caller still catches it; a *different name*,
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
        GitWriteError: When `repo_dir` resolves to the process CWD, to the root of the git
            checkout the process is running from, or to a linked worktree.
    """
    resolved = Path(repo_dir).resolve()
    if resolved == Path.cwd().resolve() or resolved == _process_repo_root():
        raise GitWriteError(
            f"note_repo_dir {repo_dir!r} resolves to {resolved} — the checkout this "
            "process is running from. Submissions create and force-push note branches to its "
            "origin, which would publish agent-authored notes into the source repository. "
            "Set CHEMCLAW_NOTE_REPO_DIR to a dedicated clone of the knowledge repo."
        )
    if (resolved / ".git").is_file():
        raise GitWriteError(
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
# raises `GitWriteError`, which `durable/publish.py` lists as non-retryable — so a throttle would
# permanently drop the note proposal instead of backing off, and the PR-gate would quietly stop
# proposing while every run reported success. The comment above claims this list is "wrong in the
# safe direction", and those two entries were the only ones that were not.
#
# Nothing is lost by removing them: a genuine denial from a forge carries a phrase as well as a
# status — GitHub's is `remote: Permission to owner/repo.git denied to user.`, matched by
# `permission denied` — so the credential cases below still classify, and a bare status line with
# no accompanying phrase stays transient, which is the safe direction for a code that has two
# meanings.
#
# **The list was surveyed against four forges on 2026-08-28, and it was GitHub-shaped.** Every
# unannotated entry below is a phrase GitHub or OpenSSH emits; run against the wordings the other
# forges actually use, five denials classified as transient and retried forever:
# GitLab's `remote: You are not allowed to push code to this project.`, Bitbucket Server's
# `remote: You are not permitted to access this resource.`, Bitbucket Cloud's `remote: Write access
# to repository not granted.`, Azure DevOps' `remote: TF401027: You need the Git
# 'GenericContribute' permission to perform this action.`, and GitHub's own SAML-SSO refusal for a
# token nobody has authorized for the organisation. Each is permanent until a human changes a
# permission, which is precisely what `GitWriteError` means.
#
# Each addition was checked the other way too — none of them appears in a throttle, a 429, a 503,
# a DNS failure or a non-fast-forward rejection, which is the property that keeps this list wrong
# in the safe direction.
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


# A forge naming the principal it refused. Matched as a pattern rather than a substring because the
# real wording puts the repository between the two words — GitHub says
# `remote: Permission to owner/repo.git denied to some-bot.`, which the substring
# `permission denied` does not match at all. That gap is why the bare status lines looked load-
# bearing: they were catching this case by accident, and catching a rate limit with it.
_DENIED_PRINCIPAL = re.compile(r"permission to .{0,200}? denied to ", re.IGNORECASE | re.DOTALL)


# How much of a git invocation may reach one log record. Both halves of the `git.failed` line are
# unbounded strings: git's stderr, and the arguments.
#
# **Neither is this repository's own text.** A `pre-receive` hook prints whatever the forge's
# administrator wrote, `remote:` lines are the remote server's output, and a chatty CI hook can
# emit kilobytes per rejected push. The argument list carries `refs/heads/<branch>`, and
# `NoteSubmission.branch` is a field on a model built from a database row — bounded now
# (`kg/record.py`), but bounded there rather than here, and defence in depth is the point.
#
# The cost of not capping is not disk: `SecretRedactingFilter` regex-scans every record's message
# **while holding the logging lock**, so an arbitrarily long field makes an arbitrarily long stall
# that every thread logging in this process waits behind — the shape of the 21 s stall this
# review measured on the same filter elsewhere in the tree. The exception keeps the full text: it
# is raised, caught and inspected, never regex-scanned under a process-wide lock.
_LOGGED_TEXT_LIMIT = 2000


def _for_log(text: str, limit: int = _LOGGED_TEXT_LIMIT) -> str:
    """`text` bounded for one log record, saying so when it was cut rather than cutting silently."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more character(s) omitted]"


def _is_auth_failure(stderr: str) -> bool:
    """Whether git's stderr says the remote refused this credential.

    The distinction `durable/publish.py` cannot make and this can: a *credential* failure on a push
    is a fact about the token, and retrying it forever is how an expired PAT becomes an
    indefinitely retrying workflow whose log says only that a publish failed.

    **A bare status code is deliberately not enough.** `403` is what a forge returns for a
    secondary rate limit as well as for a denial, and `GitWriteError` is non-retryable — so
    classifying a throttle as auth drops the note proposal instead of backing off, and the PR-gate
    stops proposing while every run still reports success. A genuine denial always says so in
    words as well, either with one of the credential phrases or by naming the principal it
    refused, so nothing is lost by requiring the words.
    """
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return True
    return _DENIED_PRINCIPAL.search(stderr) is not None


class GitNoteWriter:
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

    async def _exec(self, argv: tuple[str, ...], cwd: str | None = None) -> tuple[int, str, str]:
        """Spawn one git child and collect it under a bound; return (exit code, stdout, stderr).

        **The one place a git process is started, which is what makes the two guarantees below
        properties of this class rather than of each call site.** `_run` and `_read` both come
        through here: they differ only in which stream they want and in what a non-zero exit means,
        which is not enough difference to justify a second `create_subprocess_exec` — and the
        second one was written without the cancellation arm, so a submission cancelled while the
        tip guard was reading left a `git rev-parse` running. `_run`'s docstring had already
        asserted that could not happen.

        Bounded by `git_command_timeout_seconds`: a hung command (dead remote, credential prompt)
        is killed and reported as a failure, so it can never deadlock the process-wide submit lock
        or orphan a git child holding `.git/index.lock`.

        `cwd` is how the write half of a submission runs inside its worktree.
        `tests/test_knowledge.py` fakes `create_subprocess_exec` to prove both bounds, so a command
        issued any other way would be unbounded and invisible at once.
        """
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd if cwd is not None else self._repo_dir,
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

    async def _run(self, *args: str, cwd: str | None = None) -> tuple[int, str]:
        """Run one git command in the repo (or in `cwd`); return (exit code, stderr) — no raise.

        Stderr, because the write path only ever needs the error text. The bounds are `_exec`'s.
        """
        returncode, _stdout, stderr = await self._exec(args, cwd=cwd)
        return returncode, stderr

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

        **Both interpolated strings are bounded (`_for_log`), and the reason is that neither is
        this repository's text.** The argument that used to stand here — "the stderr is git's
        output rather than a user's text" — is wrong twice. `remote:` lines are the *remote
        server's* output, so a `pre-receive` hook or a chatty forge writes straight into this
        record at whatever length its author chose; and the same format string interpolates
        `" ".join(args)`, which on a fetch or a push carries `refs/heads/<branch>` from
        `NoteSubmission.branch`. What is true is the credential half: `SecretRedactingFilter`
        strips URL userinfo from every record, so a remote carrying `user:token@` before its host
        cannot put its credential into this line. (Written without the scheme deliberately:
        `tests/test_no_egress.py` reads every `http(s)://` literal in first-party source as a host
        this system dials, and an illustrative one in prose is indistinguishable from a real one.)
        That filter is also why the cap matters for more than disk — it regex-scans the message
        holding the logging lock, so an unbounded field is an unbounded stall for every thread
        logging in this process.
        """
        returncode, stderr = await self._run(*args, cwd=cwd)
        if returncode == 0:
            return
        auth = transient and _is_auth_failure(stderr)
        error = GitRemoteError if transient and not auth else GitWriteError
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
            retryable=error is GitRemoteError,
        )
        raise error(f"git {' '.join(args)} failed: {stderr}")

    async def _read(self, *args: str) -> str | None:
        """One git query's stdout, stripped — or `None` on a non-zero exit.

        `_run` returns stderr because the write path only ever needs the error text; the tip
        guard needs *answers* (a hash, a commit message), and reading them off stderr is how its
        first draft compared two empty strings and concluded there was nothing to lose. That is the
        whole of the difference, so the child itself is `_exec`'s — which is where the timeout and
        the kill-on-cancel live, and this used to spawn its own without the second one.
        """
        # A single argument is a ref to resolve; a full argv is run verbatim.
        argv = args if len(args) > 1 else ("rev-parse", "--verify", "--quiet", args[0])
        returncode, stdout, _stderr = await self._exec(argv)
        # A non-zero exit is an *answer* here — "no such ref" is how the tip guard learns a branch
        # does not exist yet — so it is `None` rather than a raise, which is the other half of why
        # this is not simply `_run`.
        return None if returncode != 0 else stdout

    def _contained_note_path(self, relative: str) -> Path:
        """Resolve the note path inside the notes checkout and refuse anything escaping it.

        Defense in depth behind the `Note` slug validation: even a hand-built `NoteWrite` must not
        write outside the tree. `resolve()` follows symlinks as they exist on disk, and the tree is
        materialized here — the writer commits into the checkout readers scan — so the symlink a
        committed directory could redirect the write through is resolved rather than assumed away.
        That was a live concern for the worktree this replaces, which had to be created *with* a
        checkout for the same check to mean anything.
        """
        root = Path(self._repo_dir).resolve()
        note_path = (root / relative).resolve()
        if not note_path.is_relative_to(root):
            raise GitWriteError(f"note path {relative!r} escapes the checkout {root}")
        return note_path

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Write the note's files into the checkout, commit them and push.

        Returns the commit that landed, with `written=False` when every file was byte-identical to
        what the tree already holds: there is nothing to record, so nothing is committed. A
        `repo_dir` that is the process's own checkout is refused up front
        (`_require_dedicated_checkout`) — this commits to the base branch and pushes it.
        """
        _require_dedicated_checkout(self._repo_dir)
        async with _WRITE_LOCK:
            async with self._cluster_lock():
                with _checkout_lock(self._repo_dir):
                    return await self._write_locked(write)

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
        #
        # **Named, because this connection is held across a git push.** `db.connection` times the
        # whole block and this block is the entire submission — fetch, worktree, commit and the
        # push to the remote — so the sample it books is network-to-a-forge long by construction.
        # Unnamed it landed in `chemclaw_db_query_duration_seconds{operation="unspecified"}` beside
        # actual statements and emitted a `db.slow` WARNING per submission, which reads on a
        # dashboard as database latency. The hold is real and worth measuring; what it needed was a
        # label saying what it is.
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

        **The checkout is where readers read.** `settings.notes_path` is
        `note_repo_dir / knowledge_dir`, so unlike the branch-per-note gate this replaces, these
        files land in the tree `load_notes` scans — which is exactly what makes a recorded note
        global at once, and also why this fast-forwards first: committing on a stale base would
        leave the reader a tree missing whatever another pod recorded in the meantime.

        `--ff-only` rather than a merge or a rebase: a divergence here means somebody committed
        locally in the notes clone, which is not a state this writer should resolve on its own by
        rewriting or merging history it did not author. It is raised as retryable because the
        ordinary cause is a race the next attempt re-fetches past.
        """
        branch = await self._read("symbolic-ref", "--short", "HEAD")
        if branch != self._base:
            raise GitWriteError(
                f"the notes checkout at {self._repo_dir!r} is on {branch!r}, not the base branch "
                f"{self._base!r}. Recorded notes are committed to the base branch, and readers "
                "scan this same tree, so a checkout parked elsewhere would serve the wrong notes."
            )
        await self._git("fetch", self._remote, self._base, transient=True)
        returncode, stderr = await self._run("merge", "--ff-only", f"{self._remote}/{self._base}")
        if returncode != 0:
            raise GitRemoteError(
                f"the notes checkout could not fast-forward onto {self._remote}/{self._base}: "
                f"{stderr}"
            )
        return await self._write_and_commit(write)

    async def _write_and_commit(self, write: NoteWrite) -> WriteOutcome:
        """Write the files **in order**, commit, and push the base branch.

        The order is the caller's and it is load-bearing: `record._build_write` puts dependencies
        before the subject and retirements after it, so a reader scanning mid-write never sees a
        note before what it cites. Each path is containment-checked independently — a dependency
        is no more trusted than the note.

        **This busts the graph cache, and the gate it replaces deliberately did not.** That was
        right then and is wrong now for the same reason: the gate wrote to a branch under `.git/`
        that no reader scanned, so busting would have advertised a tree change that had not
        happened. These bytes land in the tree readers do scan, so a stale cache is the difference
        between "global the moment it is learned" and "global within `graph_cache_ttl_seconds`".
        """
        written: list[str] = []
        for file in write.files:
            note_path = self._contained_note_path(file.path)
            if not file.overwrite and note_path.exists():
                continue
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(file.content, encoding="utf-8")
            written.append(file.path)
        if not written:
            return WriteOutcome(reference=self._base, written=False)
        # `--` ends option parsing before the note paths: `_contained_note_path` only checks
        # containment, and a leading-dash relative path (e.g. `-x`) resolves *inside* the repo and
        # would otherwise reach git as an option rather than a pathspec.
        await self._git("add", "--", *written)
        # Idempotent, and scoped to **our** paths: byte-identical content stages nothing.
        # `--` and the path list are load-bearing rather than tidy — a bare `diff --cached` reports
        # anything else already staged in this checkout and would turn a no-op write into a commit.
        returncode, _ = await self._run("diff", "--cached", "--quiet", "HEAD", "--", *written)
        if returncode == 0:
            return WriteOutcome(reference=self._base, written=False)
        # **Path-limited, for the reason the worktree used to supply.** The gate this replaced
        # committed inside a linked worktree with its own index, so residue staged in the shared
        # checkout structurally could not reach a note's commit. There is no second index now, so
        # the scoping has to be explicit: a plain `git commit` here commits whatever else somebody
        # left staged, into a commit named after this note.
        # `tests/test_knowledge.py::test_poisoned_index_does_not_leak_into_the_next_write` is what
        # holds it, and it is the test that found this.
        await self._git("commit", "-m", write.message, "-m", _RECORD_TRAILER, "--", *written)
        commit = await self._read("rev-parse", "HEAD")
        returncode, stderr = await self._run("push", self._remote, f"HEAD:refs/heads/{self._base}")
        if returncode != 0:
            # The remote moved inside the write window. Retryable: the next attempt fetches and
            # fast-forwards past it before writing again, and nothing here rewrites what landed.
            raise GitRemoteError(f"git push to {self._base} failed: {stderr}")
        # Only after the push, because the bytes are already readable and the cache is what makes
        # them so — but a failed push means this commit is not the record of anything yet.
        invalidate_cache()
        return WriteOutcome(reference=commit or self._base)


def default_writer() -> NoteWriter:
    """The production note writer: a commit on the notes repo's base branch. Overridden in tests."""
    return GitNoteWriter()
