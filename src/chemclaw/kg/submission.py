"""What one PR-gate submission *is*: the files it writes, and who can write them.

Split out of `chemclaw.kg.pr_gate` for a structural reason rather than a tidying one. The durable
record of a proposal (`chemclaw.kg.proposal`) has to keep the files a failed submission would have
written, or a multi-file submission cannot be replayed — and `pr_gate` already imports `proposal`,
so `proposal` cannot import `NoteFile` back from it. These three types are what both sides need
and neither owns.

`pr_gate` keeps the *policy*: which notes qualify, where they land, what the PR says, and the
record around the submit. `git_submitter` keeps the mechanics. This module is the vocabulary they
share.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class NoteFile(BaseModel):
    """One file a submission writes: where it goes and what it contains."""

    model_config = ConfigDict(frozen=True)

    path: str
    content: str


class NoteSubmission(BaseModel):
    """Everything needed to open a PR that adds one note — and whatever it depends on.

    **Why this carries a list rather than a single file.** It used to be exactly one `path` plus
    one `content`, and that single field was a structural constraint on the whole knowledge graph:
    a note could never link to another note that did not already exist on the base branch, because
    a dangling `[[wikilink]]` fails `kg-validate` on the very PR being opened.
    the `qm` bundle's note builder documented that as the reason it emitted no link at all, which is
    why computed results and the knowledge graph were disjoint stores (STO-7).

    A submission is properly a *reviewable unit*, and the unit is "this note and the notes it needs
    to make sense" — a `job-result` and the `compound` it is about. Both land in one PR, one human
    signs off on both, and the link resolves.

    `files` is ordered with the subject note first, so `files[0]` is the note the submission is
    about. Deliberately *not* also exposed as `path`/`content` convenience properties: a read-only
    property shadows anything `model_copy(update=...)` writes, so the old field names would keep
    resolving and silently ignore the update. One shape, no aliases.
    """

    branch: str
    files: list[NoteFile] = Field(min_length=1)
    title: str
    body: str


class NoteSubmitter(Protocol):
    """Submits a note as a reviewable PR and returns a reference (e.g. the PR URL).

    Contract nuance: when the note is byte-identical to what the base branch
    already contains, an implementation may return the reference *without*
    creating anything new — there is nothing to review, so re-proposing an
    unchanged note is an idempotent no-op, not an error.
    """

    async def submit(self, submission: NoteSubmission) -> str:
        """Create the branch + PR for `submission`; return a human-visible reference.

        For an unchanged note this may be the branch name without a fresh push
        (see the class docstring).
        """
        ...
