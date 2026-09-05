"""Classifying a forge's refusal, and surviving what a forge writes into a log record.

Three findings, one subject: everything on this path is somebody else's text. The refusal wordings
this gate reads are four vendors' prose, the stderr it logs is a remote server's output, and the
branch it interpolates comes from a database row.
"""

import asyncio
import logging

import pytest

from chemclaw.kg.git_writer import (
    GitNoteWriter,
    GitRemoteError,
    GitWriteError,
    _for_log,
    _is_auth_failure,
)
from chemclaw.kg.record import NoteFile, NoteWrite

# What each forge actually says when it refuses *this credential*. `GitWriteError` is in
# `durable/publish.py`'s non-retryable list, so classifying one of these as transient retries a
# push that will never succeed until a human changes a permission — and classifying a throttle as
# one of these drops the note proposal outright.
_DENIALS = {
    "github-invalid-password": (
        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://host/x'"
    ),
    "github-dead-pat": (
        "remote: Support for password authentication was removed on August 13, 2021.\n"
        "fatal: Authentication failed"
    ),
    "github-denied-principal": (
        "remote: Permission to owner/repo.git denied to some-bot.\n"
        "fatal: unable to access: The requested URL returned error: 403"
    ),
    "github-saml-sso": (
        "remote: The `acme` organization has enabled or enforced SAML SSO. To access this "
        "repository, you must use a personal access token that has been authorized."
    ),
    "gitlab-push-denied": (
        "remote: You are not allowed to push code to this project.\n"
        "fatal: unable to access: The requested URL returned error: 403"
    ),
    "gitlab-upload-denied": "remote: You are not allowed to upload code.",
    "gitlab-http-basic": (
        "remote: HTTP Basic: Access denied. The provided password or token is incorrect."
    ),
    "bitbucket-server": "remote: You are not permitted to access this resource.",
    "bitbucket-cloud": "remote: Write access to repository not granted.",
    "azure-devops-permission": (
        "remote: TF401027: You need the Git 'GenericContribute' permission to perform this action."
    ),
    "gerrit": "remote: ERROR: prohibited by Gerrit: not permitted: update",
    "ssh-publickey": (
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    ),
}

# What must stay transient. Every one of these clears without anybody touching a credential, and
# three of them carry the same 403 a denial does — which is why the status code alone is not the
# signal and why each marker above had to be checked against this table before being added.
_TRANSIENT = {
    "secondary-rate-limit": (
        "remote: You have exceeded a secondary rate limit and have been temporarily blocked from "
        "content creation.\nfatal: unable to access: The requested URL returned error: 403"
    ),
    "bare-403": "fatal: unable to access: The requested URL returned error: 403",
    "too-many-requests": "fatal: unable to access: The requested URL returned error: 429",
    "gateway": "error: RPC failed; HTTP 503 curl 22 The requested URL returned error: 503",
    "dns": "fatal: unable to access 'https://h/': Could not resolve host: h",
    "hangup": "fatal: the remote end hung up unexpectedly",
    "connect-timeout": "fatal: unable to access: Failed to connect to h port 443: timed out",
    "non-fast-forward": (
        "error: failed to push some refs\n"
        "hint: Updates were rejected because the remote contains work that you do not have"
    ),
}


@pytest.mark.parametrize("stderr", list(_DENIALS.values()), ids=list(_DENIALS))
def test_every_forge_in_the_family_has_its_denial_classified(stderr: str) -> None:
    """The marker list was GitHub-shaped; five of these twelve retried a dead credential forever.

    Surveyed 2026-08-28: GitLab, Bitbucket (Server and Cloud), Azure DevOps and Gerrit each refuse
    in their own words, and GitHub has a second wording of its own for an unauthorized SSO token.
    None of them is reachable by the phrases written for GitHub's HTTPS credential failures.
    """
    assert _is_auth_failure(stderr)


@pytest.mark.parametrize("stderr", list(_TRANSIENT.values()), ids=list(_TRANSIENT))
def test_a_fault_that_clears_on_its_own_is_never_classified_as_a_credential(stderr: str) -> None:
    """The other direction, and the reason the list must be wrong in the safe direction.

    A missed phrase costs a retry; a false positive raises `GitWriteError`, which is non-retryable,
    so it *drops* the note proposal — the PR-gate stops proposing while every run reports success.
    Three of these carry a 403 and one carries a 429; none may be read as a credential fact.
    """
    assert not _is_auth_failure(stderr)


def test_git_stderr_reaches_the_log_bounded_and_the_exception_whole(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A `pre-receive` hook writes as much as its author chose, into a record under a global lock.

    `SecretRedactingFilter` regex-scans every record's message while holding the logging lock, so an
    unbounded field is an unbounded stall for every thread logging in the process. The exception
    keeps the whole text: it is raised, caught and read, never scanned under that lock.
    """
    submitter = GitNoteWriter(repo_dir=".", base_branch="main", remote="origin")
    shouting = "remote: " + ("x" * 200_000)

    async def loud(*args: str, cwd: str | None = None) -> tuple[int, str]:
        return 1, shouting

    monkeypatch.setattr(GitNoteWriter, "_run", loud)

    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_writer"):
        with pytest.raises(GitRemoteError) as caught:
            asyncio.run(submitter._git("push", "origin", "note/x", transient=True))

    record = next(r for r in caplog.records if getattr(r, "event", "") == "git.failed")
    message = record.getMessage()
    assert len(message) < 5_000, f"{len(message)} characters of remote output reached one record"
    assert "omitted" in message, "the cut is stated rather than silent"
    assert len(str(caught.value)) > 100_000, "the exception still carries the whole of it"


def test_a_long_branch_is_bounded_in_the_log_line_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The same format string interpolates `" ".join(args)`, which carries `refs/heads/<branch>`.

    The docstring used to argue this was safe because "the stderr is git's output rather than a
    user's text" — an argument that says nothing about the argument list, and is wrong about the
    stderr as well (`remote:` lines are the remote server's).
    """
    submitter = GitNoteWriter(repo_dir=".", base_branch="main", remote="origin")

    async def failing(*args: str, cwd: str | None = None) -> tuple[int, str]:
        return 1, "fatal: no"

    monkeypatch.setattr(GitNoteWriter, "_run", failing)

    with caplog.at_level(logging.WARNING, logger="chemclaw.kg.git_writer"):
        with pytest.raises(GitWriteError):
            asyncio.run(submitter._git("fetch", "origin", "refs/heads/note/" + "b" * 100_000))

    record = next(r for r in caplog.records if getattr(r, "event", "") == "git.failed")
    assert len(record.getMessage()) < 5_000


def test_for_log_says_that_it_cut_rather_than_cutting_silently() -> None:
    """A truncated line that does not say so is read as the whole of what the remote said."""
    assert _for_log("short") == "short"
    capped = _for_log("y" * 3_000, limit=100)
    assert capped.startswith("y" * 100)
    assert "2900 more character(s) omitted" in capped


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Add note: x\nforged: log line",
        "Add note: \x00null",
        "b" * 256,
    ],
)
def test_a_commit_message_a_log_record_could_not_survive_is_refused_at_the_model(
    message: str,
) -> None:
    """Nothing bounded this field's charset or length before it reached a log record.

    Inherited verbatim from the branch-name rule this replaces
    (`D-2026-09-05-the-gate-is-deleted-not-dormant` removed the branch): `git_writer._git`
    interpolates the message into a log record, so a newline forges a log line and an unbounded one
    stalls every thread behind the redaction filter's regex scan. `record._build_write` composes it
    from a `Note.id` this repository validates, so on the shipped path the check is redundant — and
    it is not redundant against a `NoteWrite` constructed directly, which is the only reason a
    model-level constraint is the right place for it.
    """
    with pytest.raises(ValueError, match="not usable"):
        NoteWrite(
            files=[NoteFile(path="knowledge/compound/x.md", content="body\n")],
            message=message,
        )


@pytest.mark.parametrize(
    "message",
    [
        "Add job-result note: job-crash",
        "Add campaign note: bo-reizman_suzuki-a1b2 with 2 supporting note(s)",
        "Add compound note: benzene — 1,2-dichloroethane",
    ],
)
def test_the_messages_this_repository_actually_mints_are_accepted(message: str) -> None:
    """The constraint bounds control characters and length; it may not narrow what `_build_write`
    composes — including the em dash and the parenthesised supporting-note count it really emits."""
    write = NoteWrite(
        files=[NoteFile(path="knowledge/compound/x.md", content="body\n")],
        message=message,
    )
    assert write.message == message
