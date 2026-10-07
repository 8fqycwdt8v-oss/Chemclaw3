"""Writing an agent-authored note into the graph, where it is readable at once.

Knowledge is written directly and corrected rather than pre-approved
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`): it carries its provenance, is read beside
its citations, and can be contradicted or superseded. `settings.knowledge_path` is the directory
`load_notes` reads and the writer commits into, so a note is in the graph once its bytes land; the
commit provides durability and history.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.logging import redact_secrets
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.graph import dangling_links, load_notes
from chemclaw.kg.note import Note, note_relative_path
from chemclaw.kg.render import render_note

log = logging.getLogger(__name__)


class NoteFile(BaseModel):
    """One file a write puts on disk: where it goes, what it contains, and how it may replace.

    `overwrite=False` marks a *dependency* — a file included so the subject note's links resolve,
    re-rendered from source data on every write that touches it. Such a file is written only when
    the tree does not already have one: the machine rendering is byte-identical to what it minted
    before, so writing it is normally a no-op, but the moment a human has edited the copy on disk
    (hazard prose on a compound note, a tag) an unconditional write silently reverts their edit.
    The subject note keeps the default — replacing it is what a re-write *is*.

    `amendment=True` marks a *retirement* — an existing note rewritten in place with its validity
    window closed. It is a second, independent question from `overwrite`, and it exists because
    the answers differ over a note a **human** wrote: the writer refuses the subject outright
    (writing an agent note over a curated one at the same id is forgery), and leaves an amendment
    alone (the person's file is untouched and the *new* note still lands). Before the split, one
    curated file made the whole unit fail — see `git_writer._refuse_to_clobber_a_person`.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    content: str
    overwrite: bool = True
    amendment: bool = False


# What a commit subject may contain: no control characters, bounded length. The writer logs it, so a
# newline would forge a log line and an unbounded one would stall the redacting log filter.
# Redundant for validated note ids, not for a direct construction.
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
    """What a write actually did: the reference, and **how many notes it put in the graph**.

    `notes=0` is the idempotent no-op — every file was byte-identical to what the tree already
    held, so nothing was committed — and it is also the pending state of a batch that has not
    flushed. The caller acts on the difference: `chemclaw_notes_recorded_total` means "a note
    reached the graph", and incrementing it for a no-op would make it count attempts.

    **It is a count rather than a flag, because one write can carry many notes.**
    `BatchingNoteWriter` merges N notes into one commit, and against a `written: bool` the only
    honest answer for a fifty-note commit was `True` — so the counter moved by **1** where fifty
    notes had landed, measured (`D-2026-09-14-a-counter-of-commits-is-not-a-counter-of-notes`).
    A boolean cannot carry that number, and a second field beside it would be the same fact stored
    twice; `written` is therefore derived below and is exactly `notes > 0`.

    `extra="forbid"` for that reason and not for tidiness: `written=` used to be a constructor
    argument, and an ignored keyword would leave a no-op reporting one note.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    #: How many notes this write committed. 1 for the ordinary one-note-one-commit path.
    notes: int = Field(default=1, ge=0)

    @property
    def written(self) -> bool:
        """Whether anything was committed — `notes > 0`, derived rather than stored beside it."""
        return self.notes > 0


class NoteWriter(Protocol):
    """Puts a note's files in the graph and returns what happened.

    When every file is byte-identical to the tree, an implementation commits nothing and reports
    `notes=0`: re-recording an unchanged note is an idempotent no-op, not an error.
    """

    async def write(self, write: NoteWrite) -> WriteOutcome:
        """Write `write`'s files in order and commit them; return the outcome."""
        ...


def _note_file(
    note: Note, directory: str, *, overwrite: bool = True, amendment: bool = False
) -> NoteFile:
    """Where one note lands in the knowledge tree, and what is written there.

    The rendered bytes are redacted against this process's secret inventory, because a note body or
    frontmatter may carry model prose over pasted credentials, and git both leaves the pod and keeps
    history forever. Redaction rather than refusal: a false positive is visible and correctable, a
    committed credential is not.
    """
    return NoteFile(
        path=f"{directory}/{note_relative_path(note.type, note.id)}",
        content=redact_secrets(render_note(note)),
        overwrite=overwrite,
        amendment=amendment,
    )


def _build_write(
    note: Note,
    directory: str,
    dependencies: list[Note] | None,
    superseded: list[Note] | None = None,
) -> NoteWrite:
    """The files one record writes, in the order that keeps the graph readable throughout.

    Readers scan whatever is on disk, so the order is: dependencies, then the subject, then the
    retirements, each citing the one before (a `job-result` cites its `compound`; a retirement names
    its successor via `superseded-by`). Between the subject and its retirements both old and new
    notes are current; retiring first would instead leave a dangling link and a moment with no
    current note. Files are deduplicated by note id.
    """
    seen = {note.id}
    files: list[NoteFile] = []
    for dependency in dependencies or ():
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        files.append(_note_file(dependency, directory, overwrite=False))
    files.append(_note_file(note, directory))
    # Retirements overwrite: each is the note's own content with `valid_to` closed and the successor
    # named. They alone are marked `amendment`, so a retirement the writer may not make does not
    # take the subject down with it.
    for retired in superseded or ():
        if retired.id in seen:
            continue
        seen.add(retired.id)
        files.append(_note_file(retired, directory, amendment=True))

    extra = f" with {len(files) - 1} supporting note(s)" if len(files) > 1 else ""
    return NoteWrite(files=files, message=f"Add {note.type} note: {note.id}{extra}")


def _unresolved_links(note: Note, landing: list[Note], notes_dir: Path) -> list[str]:
    """`note`'s link targets that no note defines, neither on disk nor in this write.

    Uses `kg.graph.dangling_links`, the one definition, over the cached corpus. `landing` is every
    note this write puts on disk, so a subject citing a dependency written beside it resolves.
    External ids resolve in a store and are not dangling.
    """
    reported = dangling_links([*load_notes(notes_dir), *landing])
    # Deduplicated: on a re-record the subject is in both the corpus and `landing`.
    return list(dict.fromkeys(target for source, target in reported if source == note.id))


def count_notes_recorded(outcome: WriteOutcome) -> None:
    """Book what `outcome` put in the graph on `chemclaw_notes_recorded_total`.

    Counted after the writer returns, so the number means "reached the graph", not "attempted". Also
    called by `cli/backfill_corpus` for a batch's final `flush()`, which `record_note` never sees.
    """
    if outcome.notes:
        record_metric(lambda m: m.increment("chemclaw_notes_recorded_total", outcome.notes))


async def record_note(
    note: Note,
    writer: NoteWriter,
    knowledge_dir: str | None = None,
    dependencies: list[Note] | None = None,
    superseded: list[Note] | None = None,
) -> str:
    """Write an agent-authored note, with anything it links to, straight into the graph.

    Refuses a `human`-authored subject: an agent writing `created_by: human` would forge the
    provenance chemists rely on. A human-authored dependency is allowed, since it is re-rendered
    from source data and `overwrite=False` leaves an existing copy untouched.

    Stamps `actor` with the person bound to the current turn or job (the identity the audit trail
    and authorization read), and refuses a note naming a different person. With no person bound the
    field stays absent. Only the subject is stamped.

    Args:
        note: The note to record; must be `created_by == "agent"`.
        writer: How the files actually land (injected for testability).
        knowledge_dir: Override the configured notes directory.
        dependencies: Notes to write first so its links resolve.
        superseded: Retired copies of notes this one replaces; written last, and overwritten. A
            retirement of a human-written note is skipped by the writer (logged), and the subject
            still lands.

    Returns:
        The writer's reference for what landed: a commit, or the unchanged tree. A note whose
        `[[wikilinks]]` name ids nothing defines still lands, and logs a WARNING naming them.
    """
    if not note.authorship.by_agent:
        raise ValueError(
            "record_note writes agent-authored notes; a human note is written by the human"
        )
    actor = get_current_actor()
    if note.actor is not None and note.actor != actor:
        raise ValueError(
            f"note {note.id} names {note.actor!r} as the person it was written for, and this "
            f"write runs for {actor!r}; a note records the person whose turn wrote it"
        )
    if actor is not None:
        # `model_copy` rather than a re-validation: the value is `get_current_actor()`'s, which is
        # already stripped and non-blank — the only constraint the field states.
        note = note.model_copy(update={"actor": actor})

    directory = knowledge_dir if knowledge_dir is not None else settings.knowledge_dir
    # Warn about links to ids nothing defines: the note would otherwise land silently and its
    # citation fail later, and `kg-validate` runs only over this repository's corpus. A warning
    # rather than a refusal, so a typo'd citation does not lose the observation. Offloaded because
    # it reads the corpus.
    unresolved = await asyncio.to_thread(
        _unresolved_links,
        note,
        [note, *(dependencies or ()), *(superseded or ())],
        Path(settings.note_repo_dir) / directory,
    )
    if unresolved:
        log.warning(
            "note %s links to %d id(s) no note defines: %s — the note is recorded and those "
            "citations will not resolve until the notes they name exist",
            note.id,
            len(unresolved),
            ", ".join(unresolved),
        )
    outcome = await writer.write(_build_write(note, directory, dependencies, superseded))
    count_notes_recorded(outcome)
    return outcome.reference
