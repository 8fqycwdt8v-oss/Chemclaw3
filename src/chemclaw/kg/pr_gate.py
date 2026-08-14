"""The PR-gate: agent-authored notes enter the graph only via human-approved PR.

D-005 / plan step 2.7 — built once, reused everywhere (job results, campaign
narratives, distilled playbooks, report drafts). An `agent`-authored note is
validated, rendered, and submitted on a feature branch as a pull request; a human
merges. The git/GitHub mechanics sit behind the `NoteSubmitter` protocol so the
gate logic — which notes qualify, where they land, what the PR says — is one
tested function independent of how submission happens.

The types a submission is *made of* — `NoteFile`, `NoteSubmission`, `NoteSubmitter` — live in
`chemclaw.kg.submission`, because the durable proposal record needs them too and this module
already imports that one.
"""

from chemclaw.core.config import settings
from chemclaw.core.logging import redact_secrets
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, note_relative_path
from chemclaw.kg.proposal import (
    NoteProposal,
    ambient_provenance,
    record_proposal_failed,
    record_proposal_submitted,
)
from chemclaw.kg.render import render_note
from chemclaw.kg.submission import NoteFile, NoteSubmission, NoteSubmitter


def _note_file(note: Note, directory: str) -> NoteFile:
    """Where one note lands in the knowledge tree, and what is written there."""
    return NoteFile(
        path=f"{directory}/{note_relative_path(note.type, note.id)}", content=render_note(note)
    )


def _build_submission(
    note: Note, directory: str, dependencies: list[Note] | None
) -> NoteSubmission:
    """The branch, files and PR text one proposal writes — the whole of what git will see.

    Split out of `propose_note` because that function was doing five jobs in ninety lines, and
    only two of them (record, submit) are about the gate's *durability*. What a submission
    contains is a question with its own answer, and it is now testable without a submitter.

    Files are deduplicated by id with the subject note first: a caller may legitimately list the
    same dependency twice (two computed properties of one compound), and writing one path twice in
    a commit is at best noise and at worst two different renderings racing.
    """
    seen = {note.id}
    files = [_note_file(note, directory)]
    for dependency in dependencies or ():
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        files.append(_note_file(dependency, directory))

    extra = f" with {len(files) - 1} supporting note(s)" if len(files) > 1 else ""
    return NoteSubmission(
        branch=f"note/{note.id}",
        files=files,
        title=f"Add {note.type} note: {note.id}",
        body=(
            f"Agent-proposed **{note.type}** note `{note.id}`"
            + (f" (source: {note.source})" if note.source else "")
            + extra
            + ".\n\nRequires human review before merge — the agent proposes, a human decides."
        ),
    )


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
    submission = _build_submission(note, directory, dependencies)
    # The durable record, built here rather than at the eight call sites: an obligation that must
    # hold for every proposal belongs to the one wrapper they all run inside, which is the
    # placement rule the actor stamp and the job record already follow. Recording happens on *both*
    # sides of the submit because the two outcomes are the two halves of an operable gate — what is
    # awaiting review, and what never reached review at all.
    actor, session_id, correlation_id = ambient_provenance()
    proposal = NoteProposal(
        note_id=note.id,
        note_type=note.type,
        content=submission.files[0].content,
        # Everything else the submission would write. Kept because a submission is one reviewable
        # unit (D-133) and a record of one file of it can neither be replayed — the replayed note's
        # link to its compound would dangle — nor honestly shown to a reviewer.
        dependencies=tuple(submission.files[1:]),
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
        # replay. The row keeps every file's rendered bytes, so the knowledge survives the outage
        # that lost the branch — and the *unit* survives it, which storing only the subject note
        # did not: a replayed `job-result` whose `compound` was dropped links to a note that does
        # not exist and fails `kg-validate` on the PR it reopens. Recorded on every retry, which is
        # harmless — the record keys on the content, so N attempts collapse onto one row.
        # Redacted *before* it is truncated. The cut is a length bound and was described as if it
        # were also a privacy one; a realistic credential-bearing git failure — git quoting a push
        # URL with its token in the userinfo — measures 118 characters against a 300-character
        # cut, so the token was stored verbatim, in full, in a compliance table nothing prunes.
        reason = redact_secrets(str(exc))[: settings.proposal_reason_chars]
        failure = proposal.model_copy(update={"reason": reason})
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
