"""The PR-gate: agent-authored notes enter the graph only via human-approved PR.

D-005 / plan step 2.7 — built once, reused everywhere (job results, campaign
narratives, distilled playbooks, report drafts). An `agent`-authored note is
validated, rendered, and submitted on a feature branch as a pull request; a human
merges. The git/GitHub mechanics sit behind the `NoteSubmitter` protocol so the
gate logic — which notes qualify, where they land, what the PR says — is one
tested function independent of how submission happens.
"""

from typing import Protocol

from pydantic import BaseModel

from chemclaw.config import settings
from kg.note import Note
from kg.render import render_note


class NoteSubmission(BaseModel):
    """Everything needed to open a PR that adds one note to the graph."""

    branch: str
    path: str
    content: str
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


async def propose_note(
    note: Note, submitter: NoteSubmitter, knowledge_dir: str | None = None
) -> str:
    """Propose an agent-authored note through the PR-gate.

    Rejects `human`-authored notes: those are committed directly, not gated (the
    gate exists to put a human in the loop on *machine*-generated knowledge, D-005).
    Lays the note at `<knowledge_dir>/<type>/<id>.md` on a per-note branch and asks
    the submitter to open a review PR.

    Args:
        note: The note to propose; must be `created_by == "agent"`.
        submitter: How the PR is actually created (injected for testability).
        knowledge_dir: Override the configured notes directory.

    Returns:
        The submitter's reference for the opened PR. The branch is always named
        `note/<id>`, so the reference stays stable across re-proposals — including
        the unchanged-note case where the submitter skips the push.
    """
    if note.created_by != "agent":
        raise ValueError("PR-gate is for agent-authored notes; human notes commit directly")

    directory = knowledge_dir if knowledge_dir is not None else settings.knowledge_dir
    submission = NoteSubmission(
        branch=f"note/{note.id}",
        path=f"{directory}/{note.type}/{note.id}.md",
        content=render_note(note),
        title=f"Add {note.type} note: {note.id}",
        body=(
            f"Agent-proposed **{note.type}** note `{note.id}`"
            + (f" (source: {note.source})" if note.source else "")
            + ".\n\nRequires human review before merge — GxP: AI proposes, human signs off."
        ),
    )
    return await submitter.submit(submission)
