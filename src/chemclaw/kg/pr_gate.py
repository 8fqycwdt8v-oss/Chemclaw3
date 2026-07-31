"""The PR-gate: agent-authored notes enter the graph only via human-approved PR.

D-005 / plan step 2.7 — built once, reused everywhere (job results, campaign
narratives, distilled playbooks, report drafts). An `agent`-authored note is
validated, rendered, and submitted on a feature branch as a pull request; a human
merges. The git/GitHub mechanics sit behind the `NoteSubmitter` protocol so the
gate logic — which notes qualify, where they land, what the PR says — is one
tested function independent of how submission happens.
"""

from typing import Protocol

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note
from chemclaw.kg.proposal import (
    NoteProposal,
    ambient_provenance,
    record_proposal_failed,
    record_proposal_submitted,
)
from chemclaw.kg.render import render_note

# How much of a submitter's error message the record keeps. Bounded because the text is whatever
# git wrote to stderr, and an unbounded field on a compliance table is a place for a repository
# path or a token-bearing remote URL to be stored forever.
_REASON_CHARS = 300


class NoteFile(BaseModel):
    """One file a submission writes: where it goes and what it contains."""

    path: str
    content: str


class NoteSubmission(BaseModel):
    """Everything needed to open a PR that adds one note — and whatever it depends on.

    **Why this carries a list rather than a single file.** It used to be exactly one `path` plus
    one `content`, and that single field was a structural constraint on the whole knowledge graph:
    a note could never link to another note that did not already exist on the base branch, because
    a dangling `[[wikilink]]` fails `kg-validate` on the very PR being opened.
    `connectors/qm/knowledge.py` documented that as the reason it emitted no link at all, which is
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


def _note_file(note: Note, directory: str) -> NoteFile:
    """Where one note lands in the knowledge tree, and what is written there."""
    return NoteFile(path=f"{directory}/{note.type}/{note.id}.md", content=render_note(note))


async def propose_note(
    note: Note,
    submitter: NoteSubmitter,
    knowledge_dir: str | None = None,
    dependencies: list[Note] | None = None,
) -> str:
    """Propose an agent-authored note, with anything it links to, through the PR-gate.

    Rejects `human`-authored notes: those are committed directly, not gated (the
    gate exists to put a human in the loop on *machine*-generated knowledge, D-005).
    Lays the note at `<knowledge_dir>/<type>/<id>.md` on a per-note branch and asks
    the submitter to open a review PR.

    `dependencies` are notes that must exist for `note`'s links to resolve — a computed result's
    compound note, say. They ride in the same PR, which is what lets a machine-written note cite
    the thing it is about instead of naming it in prose (STO-7). Including one that is already
    merged is harmless: it renders byte-identically, so it produces no diff and the submission
    stays idempotent. A dependency that is itself `human`-authored is allowed — the gate constrains
    who may write into the graph unreviewed, and everything here is being reviewed.

    Args:
        note: The note to propose; must be `created_by == "agent"`.
        submitter: How the PR is actually created (injected for testability).
        knowledge_dir: Override the configured notes directory.
        dependencies: Notes to include alongside it so its links resolve.

    Returns:
        The submitter's reference for the opened PR. The branch is always named
        `note/<id>`, so the reference stays stable across re-proposals — including
        the unchanged-note case where the submitter skips the push.
    """
    if note.created_by != "agent":
        raise ValueError("PR-gate is for agent-authored notes; human notes commit directly")

    directory = knowledge_dir if knowledge_dir is not None else settings.knowledge_dir
    # Deduplicated by id, subject note first: a caller may legitimately list the same dependency
    # twice (two computed properties of one compound), and writing one path twice in a commit is
    # at best noise and at worst two different renderings racing.
    seen = {note.id}
    files = [_note_file(note, directory)]
    for dependency in dependencies or ():
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        files.append(_note_file(dependency, directory))

    extra = f" with {len(files) - 1} supporting note(s)" if len(files) > 1 else ""
    submission = NoteSubmission(
        branch=f"note/{note.id}",
        files=files,
        title=f"Add {note.type} note: {note.id}",
        body=(
            f"Agent-proposed **{note.type}** note `{note.id}`"
            + (f" (source: {note.source})" if note.source else "")
            + extra
            + ".\n\nRequires human review before merge — GxP: AI proposes, human signs off."
        ),
    )
    # The durable record, built here rather than at the eight call sites: an obligation that must
    # hold for every proposal belongs to the one wrapper they all run inside, which is the
    # placement rule the actor stamp and the job record already follow. Recording happens on *both*
    # sides of the submit because the two outcomes are the two halves of an operable gate — what is
    # awaiting review, and what never reached review at all.
    actor, session_id, correlation_id = ambient_provenance()
    proposal = NoteProposal(
        note_id=note.id,
        note_type=note.type,
        content=files[0].content,
        branch=submission.branch,
        actor=actor,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    try:
        reference = await submitter.submit(submission)
    except Exception as exc:
        # A submission that never reached git is the case `chemclaw_notes_publish_failures_total`
        # made countable and still left unrecoverable: the note itself was gone, with nothing to
        # replay. The row keeps the rendered bytes, so the knowledge survives the outage that lost
        # the branch. Recorded on every retry, which is harmless — the record keys on the content,
        # so N attempts at one note collapse onto one row.
        failure = proposal.model_copy(update={"reason": str(exc)[:_REASON_CHARS]})
        await record_proposal_failed(failure)
        raise
    # Counted after the submitter returns, so the number means "a note reached the branch", not "we
    # tried". A failing submitter raises, and a metric incremented before it would have reported a
    # healthy PR-gate while every write was failing — which is the exact condition this counter was
    # declared to make visible and, until now, never did.
    record_metric(lambda m: m.increment("chemclaw_notes_proposed_total"))
    # Never raises: the note has already reached the branch, and losing the record must not undo
    # the thing the record is about.
    await record_proposal_submitted(proposal.model_copy(update={"reference": reference}))
    return reference
