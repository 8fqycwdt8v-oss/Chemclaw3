"""Git-backed `NoteWriter`: commit an agent note onto the base branch and push it.

The concrete `NoteWriter` behind `kg.record.record_note`. It writes each rendered note at its path
inside the checkout, stages exactly those paths, commits them as one unit and pushes the base
branch — and that unit is the point: a note and the notes it cites reach a reader together or not
at all.

**There is no review branch, because there is no review.**
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` ended the PR-gate: knowledge is written
straight into the graph, carrying `created_by: agent`, and corrected rather than pre-approved. What
this module used to do — branch off the base, push `note/<id>`, and leave a human to open and merge
a PR — is gone along with the ~300 lines of worktree and branch machinery that made it safe. The
argument those lines rested on is *inverted* now: the note being readable through
`settings.knowledge_path` the moment it lands is the intent, not the exposure.

Two properties survive that change and are load-bearing without it:

- **The base branch is where a note lands, and the checkout stays on it.**
  `_require_dedicated_checkout` refuses a checkout this process itself runs from, and refuses a
  linked worktree: a `git commit` here mutates a working tree, and doing that under the running
  application is how a deployment loses a file nobody wrote.
- **A write is all-or-nothing in the tree.** Every path is validated before any byte is written,
  each file is replaced atomically, and any failure restores what was there — so a reader that
  walks the tree mid-write sees the old note or the new one, never half of either. The rollback
  un-stages as well as restores: a failure between the `git add` and the commit would otherwise
  leave the retracted blob in the index, where it is both publishable by anyone else's commit in
  this clone and enough to make every later write's fast-forward refuse.

Concurrency: writes in this process serialize through a module-level asyncio lock, and
cross-process ownership is *enforced* by an exclusive OS-level `flock` on a file under the
checkout's `.git/`. In production `settings.note_repo_dir` must point at a dedicated clone of the
knowledge repo.
"""

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event, secret_env_names
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import NoteError, parse_note
from chemclaw.kg.record import NoteFile, NoteWrite, NoteWriter, WriteOutcome

log = logging.getLogger(__name__)

# Serializes every write in this process — see the module docstring.
_WRITE_LOCK = asyncio.Lock()

# The advisory-lock file guarding the checkout across processes. It lives under `.git/` because
# nothing else writes there: no reader's `rglob` reaches it (`knowledge_path` is
# `note_repo_dir/knowledge_dir`), `git clean -fd` never touches it, and the deployment sidecar's
# `rsync --delete` publishes only into the knowledge directory. `deploy/knowledge-sync.sh`
# hard-codes this same relative path — the two are the same lock or they are no lock at all.
_LOCK_FILE_NAME = "chemclaw-submit.lock"

# The trailer every commit this module makes carries. It is what lets an operator tell a note this
# system recorded from one a person committed by hand, with `git log --grep` and nothing else — the
# `created_by` front-matter answers the same question for a *file*, and a commit is the unit a
# revert takes.
_RECORD_TRAILER = "Chemclaw-Note: recorded"


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


def _replace_atomically(path: Path, content: str) -> None:
    """Put `content` at `path` in one step, so a concurrent reader never sees half of it.

    `Path.write_text` truncates and then writes, and readers of this tree hold no lock — measured,
    ~80% of reads overlapping a rewrite observed a partial file, and a note whose *frontmatter*
    survived the cut parses cleanly with half its body, so its `[[wikilinks]]` are silently missing
    from the graph rather than the file being skipped. The worktree this writer replaced made that
    window structurally impossible; `os.replace` is what restores it. Same directory, because
    `os.replace` is only atomic within one filesystem.
    """
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _git_dir(repo_dir: str) -> Path:
    """The checkout's `.git` directory — a plain path computation, deliberately.

    Not `git rev-parse --git-common-dir`: `tests/test_concurrency_claims.py` builds fake `.git`
    directories that are not repositories to prove the cross-process lock is real without a
    remote, and `deploy/knowledge-sync.sh` hard-codes the same relative path so the sidecar and the
    writer take the same lock. Both would break for the sake of supporting a bare clone, which
    `_require_dedicated_checkout` refuses anyway.
    """
    return Path(repo_dir) / ".git"


@contextlib.contextmanager
def _checkout_lock(repo_dir: str) -> Iterator[None]:
    """Hold an exclusive OS-level advisory lock on the checkout for one write.

    The asyncio lock only serializes writes *within* this process. Two processes sharing
    `note_repo_dir` share one working tree and one index: the second would stage its files into the
    first's in-flight commit, and both would push the same base branch. The rollback this module
    does on failure makes it worse rather than better — restoring "what was there" would restore
    bytes the other process had just written.

    A non-blocking exclusive `flock` turns that into an immediate, actionable error. `flock` is
    tied to the open file description, so it genuinely excludes other processes and is released by
    the kernel even if this process dies mid-write.

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

    The nearest ancestor of the CWD containing `.git` — the tree a note write must never commit
    into, because it is the one the running application is checked out in.
    """
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _require_dedicated_checkout(repo_dir: str) -> None:
    """Refuse a checkout that is verifiably the process's own working tree (G4).

    The reason has been rewritten twice, and neither earlier one still applies. It was first that
    every submission began `git reset --hard` + `git clean -fd`; then that a submission force-pushed
    `note/<id>` to this checkout's `origin`, which would publish an agent-authored note into the
    ChemClaw source repository. Neither happens now.

    **What remains is the plainest form of it.** A write commits into the working tree it is handed
    and pushes that tree's base branch to its `origin`. Pointed at the checkout this process runs
    from, it commits into the running application's own source tree and pushes to the source
    repository — and `note_repo_dir` still defaults to `"."`, so the mistake costs one unset
    environment variable.

    Also refuses a `repo_dir` whose `.git` is a *file* rather than a directory, i.e. a linked
    worktree: the write lock is a plain path under `<repo>/.git/`, and inside a linked worktree
    that is a pointer file — which surfaces later as a confusing failure rather than here as a
    clear one.

    Raises:
        GitWriteError: When `repo_dir` resolves to the process CWD, to the root of the git
            checkout the process is running from, or to a linked worktree.
    """
    resolved = Path(repo_dir).resolve()
    if resolved == Path.cwd().resolve() or resolved == _process_repo_root():
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
# permanently drop the note instead of backing off, and this system would quietly stop
# recording knowledge while every run reported success. The comment above claims this list is
# "wrong in the safe direction", and those two entries were the only ones that were not.
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


# What git says when it cannot take the index lock, matched on the two halves that are git's own
# text rather than on the path between them. It is the only *local* git failure in this module that
# a retry can clear on its own, and the phrase git prints under it says so: "a git process may have
# crashed in this repository earlier: remove the file manually to continue."
_INDEX_LOCK_MARKERS = ("index.lock", "file exists")


def _is_a_contended_index(stderr: str) -> bool:
    """Whether git refused because `.git/index.lock` already exists.

    **Local, and still retryable — the one exception to the split `_git` otherwise makes.** Every
    other `add`/`commit` failure is structural and replays identically, which is what puts
    `GitWriteError` in `durable/publish._BAD_DATA_TYPES`. This one is a *holder*: a git child that
    is still running, or a lock file a killed one left behind — and `_clear_a_stale_index_lock`
    below removes the stale file at the top of the next write, so the retry `GitRemoteError` buys
    is what reaches that reconciliation. Classified non-retryable it was measured as a permanent
    wedge: `note_write_max_attempts` was never spent, `publish_note_best_effort` swallowed the
    first attempt, and every note on that pod was dropped until a human deleted a file.
    """
    lowered = stderr.lower()
    return all(marker in lowered for marker in _INDEX_LOCK_MARKERS)


def _is_auth_failure(stderr: str) -> bool:
    """Whether git's stderr says the remote refused this credential.

    The distinction `durable/publish.py` cannot make and this can: a *credential* failure on a push
    is a fact about the token, and retrying it forever is how an expired PAT becomes an
    indefinitely retrying workflow whose log says only that a publish failed.

    **A bare status code is deliberately not enough.** `403` is what a forge returns for a
    secondary rate limit as well as for a denial, and `GitWriteError` is non-retryable — so
    classifying a throttle as auth drops the note instead of backing off, and this system stops
    recording knowledge while every run still reports success. A genuine denial always says so in
    words as well, either with one of the credential phrases or by naming the principal it
    refused, so nothing is lost by requiring the words.
    """
    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return True
    return _DENIED_PRINCIPAL.search(stderr) is not None


class GitNoteWriter:
    """Commit a note onto the base branch via git. Conforms to `NoteSubmitter`.

    Not a per-note branch: `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted the
    review the branch existed to feed, and this class's own module docstring has said so since.
    The sentence that stood here outlived it by describing the mechanism rather than the purpose,
    which is why it read as current — `_require_dedicated_checkout` refuses any branch but the
    base, so the shape it promised is one the code now rejects.
    """

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

        **The one place a git process is started, which is what makes the two guarantees below
        properties of this class rather than of each call site.** `_run` and `_read` both come
        through here: they differ only in which stream they want and in what a non-zero exit means,
        which is not enough difference to justify a second `create_subprocess_exec` — and the
        second one was written without the cancellation arm, so a write cancelled while the
        tip guard was reading left a `git rev-parse` running. `_run`'s docstring had already
        asserted that could not happen.

        Bounded by `git_command_timeout_seconds`: a hung command (dead remote, credential prompt)
        is killed and reported as a failure, so it can never deadlock the process-wide write lock
        or leave a git *child* running.

        **It can leave that child's `.git/index.lock` behind, and this docstring used to claim it
        could not.** The kill is `SIGKILL`, so a `git commit` taking the index dies without its own
        cleanup — measured, the lock file survives. Nothing here can prevent that (a lock file is
        precisely what a process that may be killed cannot unwind); what the module does instead is
        classify the resulting failure as retryable (`_is_a_contended_index`) and clear the stale
        file under the flock on the next write (`_clear_a_stale_index_lock`).

        `tests/test_knowledge.py` fakes `create_subprocess_exec` to prove both bounds, so a command
        issued any other way would be unbounded and invisible at once.
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
        """Run one git command in the repo; return (exit code, stderr) — no raise.

        Stderr, because the write path only ever needs the error text. The bounds are `_exec`'s.
        """
        returncode, _stdout, stderr = await self._exec(args)
        return returncode, stderr

    async def _git(self, *args: str, transient: bool = False) -> None:
        """Run one git command, raising on a non-zero exit — and log what git actually said.

        `transient=True` marks the commands whose ordinary failure is the *network's* — fetch and
        push — so they raise the retryable `GitRemoteError`. Local operations (add, commit,
        checkout) fail for structural reasons a retry replays identically, and keep the
        non-retryable class — **with one exception, which is a holder rather than a structure**: a
        contended `.git/index.lock` is retryable wherever it appears (`_is_a_contended_index`).

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
        `" ".join(args)`, which on a fetch or a push carries `refs/heads/<branch>` from the
        configured base branch. What is true is the credential half: `SecretRedactingFilter`
        strips URL userinfo from every record, so a remote carrying `user:token@` before its host
        cannot put its credential into this line. (Written without the scheme deliberately:
        `tests/test_no_egress.py` reads every `http(s)://` literal in first-party source as a host
        this system dials, and an illustrative one in prose is indistinguishable from a real one.)
        That filter is also why the cap matters for more than disk — it regex-scans the message
        holding the logging lock, so an unbounded field is an unbounded stall for every thread
        logging in this process.
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
        That was already a live concern for the worktree this replaces, which had to be created
        *with* a checkout for the same check to mean anything; it is more direct now, because the
        tree the write lands in is the tree every reader walks.

        **Containment alone is not enough, because `.git/` is inside the checkout.** A path like
        `.git/config` or `.git/hooks/pre-commit` resolves *within* `root` and would pass the test
        above — the first turns a note write into control over where this checkout pushes, the
        second into arbitrary execution on the next commit. Nothing reaches here with such a path
        today (`record._note_file` builds every one through `note_relative_path`, whose segments are
        slug-validated), which is exactly the argument that made the containment check "defense in
        depth" — and depth that stops one directory short of the interesting one is not depth. The
        write lock file lives under `.git/` too, so this also refuses a note that would overwrite
        the lock guarding its own write.
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
        """Serialize writes to one remote across pods, via a Postgres advisory lock.

        The `flock` below is host-local: each pod's clone lives in its own `emptyDir`, so N pods
        held N independent locks against one origin, and two pods proposing the same note id
        concurrently were last-writer-wins with no error. The database every durable deployment
        already shares is the one mutual ground, so where it is configured
        (`session_store="postgres"`), submissions take a session-level advisory lock keyed on the
        remote URL for the duration of the submit. `pg_advisory_lock` *queues* rather than fails,
        which is the right shape — a waiting pod waits exactly as long as the submission it would
        otherwise have raced. The wait is bounded by the connection's statement timeout, and a
        timeout raises the retryable `GitRemoteError`.

        **What it is gated on, and what that leaves.** `session_store` defaults to `memory`, and a
        memory-store deployment (the CLI, tests) is single-process by construction, so skipping the
        lock there is right rather than a gap. The shipped chart sets `CHEMCLAW_SESSION_STORE:
        postgres`, so a multi-pod OpenShift deployment does take it. What is uncovered is the
        combination in between — several writer pods with a memory session store — which is the same
        misconfiguration `values.yaml` flags for `framingEnvelopeSecret`, and `BACKLOG.md` carries
        the open question of refusing it at startup.

        Its consequence is now much smaller than it was, which is why this is a note rather than a
        second mechanism: two pods racing produce a divergence, and `_replay_our_unpushed_commits`
        resolves a divergence on the next write instead of wedging the pod. Before that, an
        unguarded race left a note stranded on one pod's disk permanently.
        """
        if settings.session_store != "postgres":
            yield
            return
        from chemclaw.core import db

        returncode, url = await self._run("config", "--get", f"remote.{self._remote}.url")
        identity = url if returncode == 0 and url else f"{self._repo_dir}:{self._remote}"
        key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
        # The acquisition failures are wrapped; the write body's own exceptions are not —
        # a broad handler around the `yield` would relabel a genuine write error as a lock one.
        #
        # **Named, because this connection is held across a git push.** `db.connection` times the
        # whole block and this block is the entire write — fetch, commit and the push to the
        # remote — so the sample it books is network-to-a-forge long by construction.
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

        **The checkout is where readers read.** `settings.knowledge_path` is
        `note_repo_dir / knowledge_dir`, so unlike the branch-per-note gate this replaces, these
        files land in the tree `load_notes` scans — which is exactly what makes a recorded note
        global at once, and also why this fast-forwards first: committing on a stale base would
        leave the reader a tree missing whatever another pod recorded in the meantime.

        `--ff-only` first, and a **rebase of our own unpushed commits** when that fails — never a
        merge, and never a rebase of anything this writer did not author.

        The reason the fast-forward alone is not enough is a state this module creates on purpose.
        A push that fails leaves the note committed locally and readable here
        (`tests/test_knowledge.py` asserts exactly that); the moment the remote then moves, the
        clone has diverged, `--ff-only` refuses, and **every subsequent write on this pod fails
        forever** — while `_push`'s own docstring says the next attempt "fetches, fast-forwards past
        whatever landed, and pushes this commit along with its own". It cannot; it could not; the
        first failed push wedged the pod.

        Rebasing is the operation that makes that sentence true, and it is safe exactly while every
        commit being replayed is one of ours: unpushed, and carrying `_RECORD_TRAILER`. A local
        commit *without* that trailer is a person's — somebody working in the notes clone by hand —
        and moving it is not this writer's call, so that is the case the refusal is kept for. Which
        is what the paragraph this replaces was really protecting; it just could not tell the two
        apart, so it refused both and one of them was itself.

        **And a rebase cannot resolve what a rebase cannot see.** Both recoveries above are about
        *commits*; a hard kill leaves residue that is not a commit — blobs staged and never
        committed, a `.git/index.lock` nobody released — and every in-process handler that would
        have cleared it is exactly what a `SIGKILL` does not run. Two more steps therefore run
        here, where the flock that makes them safe is held:
        `_clear_a_stale_index_lock` before anything touches the index, and
        `_discard_a_dead_writes_residue` between the failed fast-forward and the rebase, because
        that is the point at which git has said the checkout cannot move and the residue is what
        is holding it.
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

        `_exec` kills its child on a timeout and on cancellation, and the kill is `SIGKILL`: the
        child dies without unwinding, and measured, the *lock file* survives it. So does the lock a
        `git commit` held when the pod itself was evicted. Nothing in this module, in
        `deploy/knowledge-sync.sh` or in any startup path removed it, and every `git add` in the
        checkout then failed — permanently, and non-retryably until `_is_a_contended_index`, so
        `note_publish_retry` spent no attempt and `publish_note_best_effort` dropped every note.

        **Two conditions, and each covers what the other cannot.** The exclusive `flock` is held
        here, which is the evidence that no *peer writer* is mid-write; it says nothing about a
        person running `git commit` in the clone by hand, who takes no flock. The age bound is what
        covers that one: the lock is removed only when it is older than the ceiling every git child
        of this module is held to (`git_command_timeout_seconds`), so a command that could still be
        running keeps its index. A lock younger than that raises the retryable `GitRemoteError`
        instead, and the retry is what reaches this once the lock has aged into staleness.

        Best-effort by construction: if the unlink races something, the `git add` that follows
        fails with the retryable class and the next attempt tries again.
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

        **The state this recovers is defined by a handler not running.** `_write_and_commit`
        unwinds a partial write in an `except BaseException` — restore the tree, un-stage the blobs
        — and a `SIGKILL` runs neither that clause nor any `finally`, so a pod eviction between the
        `git add` and the commit leaves the note's blobs staged and its bytes in the tree with
        nothing anywhere to clear them. Measured: once another pod pushes the same note paths,
        `merge --ff-only` refuses, `_replay_our_unpushed_commits` finds no commits of ours to
        replay and refuses too, and **every later write on this pod fails forever** — three
        unrelated notes in a row, index still staged after each. The sidecar cannot recover it
        either: `knowledge-sync.sh::refresh_note_repo` fast-forwards with the same `--ff-only` and
        treats a divergence as a warning by design, so it logs once per interval and changes
        nothing.

        **Called only when the fast-forward has already failed, and that is the whole of what makes
        it acceptable.** A staged note in this clone is indistinguishable from residue — that is
        what a kill leaves — so a sweep that ran on every write would have to discard an operator's
        staged work as well, which `test_poisoned_index_does_not_leak_into_the_next_write` refuses
        and is right to: while the checkout still moves, nothing here is owed that. Once git itself
        says the checkout cannot move, the alternative to discarding is that this pod records no
        knowledge at all, ever again. So the trade is taken exactly there and nowhere else, and it
        is logged at WARNING with the paths, because a person who staged a note by hand is the one
        holder this cannot tell from a corpse.

        Scoped to `knowledge_dir`, which is every path this writer can stage: `record._note_file`
        builds them all as `<knowledge_dir>/…`. A blocker outside it is not this writer's residue
        and is left alone, to be reported by the refusal that already names it.
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
        # Index back to `HEAD` for those paths. Un-staging alone does not clear the wedge —
        # measured: the residue is then *untracked*, and `merge --ff-only` refuses to overwrite an
        # untracked file just as it refuses a staged one. The worktree has to go back too.
        await self._git("reset", "-q", "HEAD", "--", *orphaned)
        # Of those paths, the ones `HEAD` actually holds — the index now agrees with `HEAD`, so
        # this is exactly the set `git checkout --` can restore. The rest existed only in the dead
        # write, and restoring those means removing them.
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
        """Rebase this clone's own unpushed note commits onto the remote — or refuse.

        Called only when the fast-forward failed, which means this clone holds commits the remote
        does not. Every one of them must carry `_RECORD_TRAILER`: those are this writer's, stranded
        by a push that failed, and replaying them is what lets the pod recover instead of failing
        every write from then on. Anything else is a person's commit in the notes clone, and
        rewriting it is not a decision this writer gets to take on its own.

        **"No commits at all" is a third case and gets its own sentence.** A fast-forward also
        fails on a dirty tree or a dirty index, and the arithmetic below then reports
        `0 local commit(s) this system did not write` — a refusal naming commits that do not
        exist, which sends an operator looking for the wrong thing entirely.
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
        # **Every path is resolved and checked before any byte is written.** A containment failure
        # on the third file used to leave the first two on disk, readable as knowledge by a scan
        # that no longer has a gate in front of it — so validation is a separate pass.
        planned: list[tuple[Path, NoteFile]] = []
        for file in write.files:
            note_path = self._contained_note_path(file.path)
            if not file.overwrite and note_path.exists():
                continue
            # An amendment to somebody's own note is dropped rather than refused, and only an
            # amendment: the subject note keeps the hard refusal, because writing an agent note
            # over a curated one at the same id is the forgery the check exists for. See
            # `_refuse_to_clobber_a_person` for why the unit must not die with the amendment.
            if file.amendment and self._is_a_persons_note(note_path):
                log_event(
                    log,
                    "kg.write.amendment_left_alone",
                    "left %s alone: it is a human's note, so it is not retired in place — the "
                    "note recorded alongside it still lands and marks it as contradicted",
                    file.path,
                    level=logging.WARNING,
                    path=file.path,
                )
                continue
            self._refuse_to_clobber_a_person(note_path, file.path)
            planned.append((note_path, file))
        if not planned:
            return WriteOutcome(reference=self._base, written=False)

        # What each target held before this write, so a failure before the commit can put the tree
        # back. `None` means the file did not exist.
        prior = {path: (path.read_bytes() if path.exists() else None) for path, _ in planned}
        written = [file.path for _, file in planned]
        try:
            for note_path, file in planned:
                note_path.parent.mkdir(parents=True, exist_ok=True)
                _replace_atomically(note_path, file.content)
            # `--` ends option parsing before the note paths: `_contained_note_path` only checks
            # containment, and a leading-dash relative path (e.g. `-x`) resolves *inside* the repo
            # and would otherwise reach git as an option rather than a pathspec.
            await self._git("add", "--", *written)
            # Idempotent, and scoped to **our** paths: byte-identical content stages nothing.
            # `--` and the path list are load-bearing rather than tidy, and the failure they
            # prevent is the *loud* one rather than the quiet one first written here. A bare
            # `diff --cached` reports anything else already staged, so it would send a no-op write
            # into the `git commit` below — which is itself path-limited, so git finds nothing to
            # commit on those paths and exits non-zero. Measured: a byte-identical re-write goes
            # from `written=False` to a non-retryable `GitWriteError`, and `durable/publish.py`
            # drops the note. `tests/test_knowledge.py` drives that combination.
            returncode, _ = await self._run("diff", "--cached", "--quiet", "HEAD", "--", *written)
            if returncode != 0:
                # **Path-limited, for the reason the worktree used to supply.** The gate this
                # replaced committed inside a linked worktree with its own index, so residue staged
                # in the shared checkout structurally could not reach a note's commit. There is no
                # second index now, so the scoping has to be explicit: a plain `git commit` here
                # commits whatever else somebody left staged, into a commit named after this note.
                # It is also what makes the `diff --cached` scoping above fail loudly instead of
                # silently — see the comment there.
                await self._git(
                    "commit", "-m", write.message, "-m", _RECORD_TRAILER, "--", *written
                )
        except BaseException:
            # The bytes are in the tree readers scan, so leaving a half-written unit there is
            # publishing it. Restore what each target held and re-raise.
            for note_path, content in prior.items():
                if content is None:
                    note_path.unlink(missing_ok=True)
                else:
                    _replace_atomically(note_path, content.decode("utf-8"))
            # **The tree is only half of what this write touched.** If the failure came after the
            # `git add` above — a `pre-commit` hook, an `index.lock`, `_exec`'s timeout kill — the
            # index still holds the blob just retracted, and restoring the tree does not remove it.
            # Left staged it is two failures: any non-path-limited `git commit` in this clone
            # publishes a note this system un-published, and a later `merge --ff-only` refuses
            # because of it — which `_replay_our_unpushed_commits` sees as a checkout with no
            # commits of ours to replay, so it refuses too and every later write on this pod fails
            # forever.
            #
            # **The middle clause of that sentence used to say "the next write's", and measured it
            # is narrower than that.** The fast-forward refuses only once the incoming commits
            # touch the staged paths; with the remote unchanged a following write fast-forwards
            # cleanly and its path-limited commit ignores the residue entirely. So the wedge is the
            # *two-pod* case — which is the one `_cluster_lock` exists for, not a corner. Being
            # precise about it is what shows why this handler is not enough on its own: it is the
            # kill that skips this clause that produces the state, and
            # `_discard_a_dead_writes_residue` is where that is undone.
            #
            # Scoped to `written` for the same reason the commit is: nothing else somebody staged
            # here is this writer's to reset.
            with contextlib.suppress(ChemclawError):
                await self._run("reset", "-q", "HEAD", "--", *written)
            invalidate_cache()
            raise

        # **Busted here rather than after the push, and unconditionally.** These bytes are in the
        # tree `load_notes` scans, so a warm in-process reader that does not rescan is serving a
        # graph without the note — including on the path where the push then fails and the note is
        # readable locally, which is exactly the state a stale cache hides.
        invalidate_cache()
        commit = await self._read("rev-parse", "HEAD")
        return await self._push(commit)

    async def _push(self, commit: str | None) -> WriteOutcome:
        """Push the base branch, and report `written` by what the *remote* now has.

        **Separated so the no-diff path can reach it too.** A push that fails leaves a commit on the
        local base branch; the retry rewrites the same bytes, stages nothing, and used to return
        `written=False` without ever pushing — so a transient rejection stranded the note on one
        pod's disk, reported success to the caller and never reached the remote or any other
        reader. The idempotence that makes a re-record cheap must not also swallow the un-pushed
        commit, so what decides `written` is whether the local base is ahead of its remote-tracking
        ref, not whether *this* call staged anything.
        """
        ahead = await self._read("rev-list", "--count", f"{self._remote}/{self._base}..HEAD")
        if ahead == "0":
            return WriteOutcome(reference=commit or self._base, written=False)
        try:
            # Through `_git`, so a push reaches the classifier written for it. Every wording in
            # `_AUTH_FAILURE_MARKERS` is a *push*-side refusal, and this raised its own
            # `GitRemoteError` directly — so all five were unreachable, a read-only token retried
            # `note_write_max_attempts` times against a credential no retry installs, and git's
            # own stderr never reached the `git.failed` record that exists to carry it.
            await self._git("push", self._remote, f"HEAD:refs/heads/{self._base}", transient=True)
        except GitWriteError as exc:
            # The class `_git` chose is kept — retryable for a network failure or a rejection,
            # non-retryable for a denied credential — and what is added is the half the caller
            # cannot see. The note is already committed on the base branch of the tree readers
            # scan, so "the write failed" is false where it matters most: told that, the model
            # retried five times permuting its arguments and then printed the document into the
            # chat (`GitWriteError`'s docstring records the measurement). It is the *push* that
            # failed, and the next attempt pushes this same commit.
            raise type(exc)(
                f"{exc} — the note is committed on {self._base} in this checkout and is already "
                f"readable here; only the push to {self._remote} did not happen, so re-record "
                "nothing and change nothing: the next attempt pushes this same commit."
            ) from exc
        return WriteOutcome(reference=commit or self._base)

    def _is_a_persons_note(self, note_path: Path) -> bool:
        """Whether a human's note is already at `note_path` — the check both policies below read.

        An unparseable or absent file is not a person's work, so it is not one.
        """
        if not note_path.exists():
            return False
        try:
            existing = parse_note(note_path)
        except (NoteError, OSError):
            return False
        return existing.created_by == "human"

    def _refuse_to_clobber_a_person(self, note_path: Path, relative: str) -> None:
        """Refuse to overwrite a note a human authored.

        **The control the PR-gate used to be.** `record_note` checks `created_by` on the note it is
        *given*, which says nothing about what is already at that path — so an agent write to an id
        a chemist curated replaced their file in place, and a retirement rewrote a human's note to
        close its validity window. Under the gate a reviewer saw "this modifies a human-authored
        file" in the diff and could refuse; nothing sees it now.

        The system's own answer to disagreeing with curated knowledge is a *new* note carrying a
        `contradicts` edge (`memory/failure.py`), which is what the deletion ADR names as the
        replacement control — and that control only works while the thing to be contradicted is
        still there.

        **Which is why this refuses the subject note and not an amendment.** A `NoteFile` marked
        `amendment=True` is a retirement of a note that already exists, and refusing the *write*
        for it took the new note down with it: `record_failure` puts the failure note and the
        retirement in one unit, so a chemist refuting a curated playbook — 37 of the 38 notes in
        the shipped corpus are `created_by: human` — lost the observation as well as the date. The
        amendment steps aside in `_write_and_commit` instead, which leaves exactly the state
        `close_refuted_note` documents as the truthful one for a claim this system may not close:
        the note stays open, served, and permanently marked as contradicted.
        """
        if self._is_a_persons_note(note_path):
            raise GitWriteError(
                f"refusing to overwrite {relative!r}: it is authored by a human, and an agent "
                "write may not replace or retire curated knowledge in place. Record a new note "
                "that contradicts or supersedes it instead."
            )


def default_writer() -> NoteWriter:
    """The production note writer: a commit on the notes repo's base branch. Overridden in tests."""
    return GitNoteWriter()
