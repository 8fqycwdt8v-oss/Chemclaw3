"""Writing an agent-authored note into the graph, where it is readable at once.

This replaces the PR-gate (`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`). D-005 put a
human in front of everything an agent writes, under a premise
`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks` removed; the
axis now is whether a thing *changes what the agent does*. Knowledge does not — it arrives labelled
with its provenance (D-160), it is read as evidence beside its own citations, and it can be
contradicted (`memory/failure.py`'s `contradicts` edge, `kg/conflicts.py`, `memory/supersede.py`,
bi-temporal `valid_to`). **Correction, not pre-approval, is the control on knowledge.**

**Why a file write is enough to make it global.** `settings.notes_path` is
`note_repo_dir / knowledge_dir` — the one location `load_notes` reads and this module writes, which
is the property `chemclaw.core.config.kg` introduced it for. So a note is in the graph the
moment its bytes land; the commit that follows is durability and history, not publication.

**The vocabulary lives here rather than in a module of its own.** `submission.py` existed because
the durable proposal record had to hold the files a failed submission would have written, and
`pr_gate` already imported `proposal`, so the types could not live in either. That record is gone,
and with it the reason for the split.
"""

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, note_relative_path
from chemclaw.kg.render import render_note


class NoteFile(BaseModel):
    """One file a write puts on disk: where it goes, what it contains, and whether it may replace.

    `overwrite=False` marks a *dependency* — a file included so the subject note's links resolve,
    re-rendered from source data on every write that touches it. Such a file is written only when
    the tree does not already have one: the machine rendering is byte-identical to what it minted
    before, so writing it is normally a no-op, but the moment a human has edited the copy on disk
    (hazard prose on a compound note, a tag) an unconditional write silently reverts their edit.
    The subject note keeps the default — replacing it is what a re-write *is*.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    content: str
    overwrite: bool = True


# What a commit subject may contain, checked here rather than at the subprocess: `git_writer._git`
# interpolates it into a log record, so a newline forges a log line and an unbounded one stalls
# every thread behind `SecretRedactingFilter`'s regex scan under the logging lock. That reasoning
# is inherited verbatim from the branch-name rule this replaces — the subject is built from a
# `Note.id` this repository validates, so on the shipped path it is redundant, and it is not
# redundant against a direct construction.
_MESSAGE = re.compile(r"[^\x00-\x1f\x7f]+")
_MAX_MESSAGE_LENGTH = 255


class NoteWrite(BaseModel):
    """One note and everything that must land with it, in the order it lands.

    `files` is ordered and the order is load-bearing rather than cosmetic — see `_build_write`.
    """

    files: list[NoteFile] = Field(min_length=1)
    message: str

    model_config = ConfigDict(frozen=True)

    def model_post_init(self, _: object) -> None:
        """Refuse a commit subject that a log line could not survive."""
        if not _MESSAGE.fullmatch(self.message) or len(self.message) > _MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"commit message {self.message[:80]!r} is not usable: it must be a non-empty "
                f"single line of at most {_MAX_MESSAGE_LENGTH} characters with no control "
                "characters"
            )


class WriteOutcome(BaseModel):
    """What a write actually did: the reference, and whether anything changed on disk.

    `written=False` is the idempotent no-op — every file was byte-identical to what the tree
    already held, so nothing was committed. The caller acts on the difference: the counter below
    means "a note reached the graph", and incrementing it for a no-op would make it count attempts.
    """

    model_config = ConfigDict(frozen=True)

    reference: str
    written: bool = True


class NoteWriter(Protocol):
    """Puts a note's files in the graph and returns what happened.

    Contract nuance: when every file is byte-identical to what the tree already holds, an
    implementation returns `written=False` without committing anything — re-recording an unchanged
    note is an idempotent no-op, not an error.
    """

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Write `write`'s files in order and commit them; return the outcome."""
        ...


def _note_file(note: Note, directory: str, *, overwrite: bool = True) -> NoteFile:
    """Where one note lands in the knowledge tree, and what is written there."""
    return NoteFile(
        path=f"{directory}/{note_relative_path(note.type, note.id)}",
        content=render_note(note),
        overwrite=overwrite,
    )


def _build_write(
    note: Note,
    directory: str,
    dependencies: list[Note] | None,
    superseded: list[Note] | None = None,
) -> NoteWrite:
    """The files one record writes, **in the order that keeps the graph readable throughout**.

    A PR made this question moot: every file of a submission merged in one commit, so no reader
    ever saw half of it. Writing directly, a reader can, and `load_notes` runs against whatever is
    on disk at that instant. So the order is the invariant that replaces "one PR is one reviewable
    unit" (D-133), and it is:

    **dependencies, then the subject, then the retirements** — because each cites the one before
    it. A `job-result` cites its `compound`, so the compound is there first and the subject never
    appears in the graph before what it cites. A retirement cites its *successor* through
    `superseded-by`, so it lands after the subject exists.

    The cost of that order is stated rather than hidden: between the subject's write and its
    retirements', the old note and its replacement are both current, and retrieval can serve both.
    That is the lesser of the two windows. Retiring first would leave `superseded-by` pointing at a
    note that does not exist yet — a dangling wikilink is what `kg-validate` exists to prevent —
    and would leave an instant with *no* current note on the subject at all.

    Files are deduplicated by note id: a caller may legitimately list the same dependency twice
    (two computed properties of one compound), and writing one path twice in a commit is at best
    noise and at worst two renderings racing.
    """
    seen = {note.id}
    files: list[NoteFile] = []
    for dependency in dependencies or ():
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        files.append(_note_file(dependency, directory, overwrite=False))
    files.append(_note_file(note, directory))
    # Retirements *do* overwrite: each is the file's own content (human edits included) with
    # `valid_to` closed and the successor named, and rewriting that copy is the point.
    for retired in superseded or ():
        if retired.id in seen:
            continue
        seen.add(retired.id)
        files.append(_note_file(retired, directory))

    extra = f" with {len(files) - 1} supporting note(s)" if len(files) > 1 else ""
    return NoteWrite(files=files, message=f"Add {note.type} note: {note.id}{extra}")


async def record_note(
    note: Note,
    writer: NoteWriter,
    knowledge_dir: str | None = None,
    dependencies: list[Note] | None = None,
    superseded: list[Note] | None = None,
) -> str:
    """Write an agent-authored note, with anything it links to, straight into the graph.

    **Refuses a `human`-authored note, and the reason is not the one it used to be.** The gate
    refused one because human notes took a different path; now every note takes this path, and the
    refusal is the only thing keeping `created_by` honest — an agent writing `created_by: human`
    would be forging the provenance that D-160 put on the evidence sweep, which is what lets a
    chemist tell curated knowledge from machine-written knowledge at the point of use. A
    *dependency* that is human-authored stays allowed: it is re-rendered from source data, not
    authored here, and `overwrite=False` leaves an existing copy exactly as the human left it.

    Args:
        note: The note to record; must be `created_by == "agent"`.
        writer: How the files actually land (injected for testability).
        knowledge_dir: Override the configured notes directory.
        dependencies: Notes to write first so its links resolve.
        superseded: Retired copies of notes this one replaces; written last, and overwritten.

    Returns:
        The writer's reference for what landed — a commit, or the unchanged tree.
    """
    if note.created_by != "agent":
        raise ValueError(
            "record_note writes agent-authored notes; a human note is written by the human"
        )

    directory = knowledge_dir if knowledge_dir is not None else settings.knowledge_dir
    outcome = await writer.write(_build_write(note, directory, dependencies, superseded))
    if outcome.written:
        # Counted after the writer returns, so the number means "a note reached the graph" rather
        # than "we tried" — the distinction `chemclaw_notes_proposed_total` was declared to make
        # and, until the gate was measured, did not.
        record_metric(lambda m: m.increment("chemclaw_notes_recorded_total"))
    return outcome.reference
