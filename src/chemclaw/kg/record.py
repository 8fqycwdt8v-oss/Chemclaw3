"""Writing an agent-authored note into the graph, where it is readable at once.

Knowledge is written directly and corrected rather than pre-approved
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`): it carries its provenance, is read beside
its citations, and can be contradicted or superseded. `settings.knowledge_path` is the directory
the note store reads and the writer commits into, so a note is in the graph once its bytes land;
the commit provides durability and history.

`settings.kg_store` picks where a note goes. `git` writes the tree only. `dual` writes the tree
first and then Postgres: the tree stays the authority, so its failure is the caller's, while a
Postgres failure is logged, counted and left for the comparison to find, and Postgres can never be
ahead of the tree. `postgres` writes the database only.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.logging import log_event, redact_secrets
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.graph import dangling_links
from chemclaw.kg.note import Note, NoteError, note_relative_path
from chemclaw.kg.render import render_note
from chemclaw.kg.rows import canonical_note, content_sha
from chemclaw.kg.store import NoteUnit, PutOutcome, note_store, postgres_note_store

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


def _unit(
    note: Note,
    dependencies: list[Note] | None,
    superseded: list[Note] | None,
    retired_from: Mapping[str, str] | None = None,
) -> NoteUnit:
    """The notes of one record in write order: dependencies, then the subject, then retirements."""
    return NoteUnit(
        note, tuple(dependencies or ()), tuple(superseded or ()), retired_from=retired_from or {}
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
    unit = _unit(note, dependencies, superseded)
    # Retirements overwrite: each is the note's own content with `valid_to` closed and the successor
    # named. They alone are marked `amendment`, so a retirement the writer may not make does not
    # take the subject down with it.
    files = [
        _note_file(
            member,
            directory,
            overwrite=role != "dependency",
            amendment=role == "retirement",
        )
        for role, member in unit.ordered()
    ]

    extra = f" with {len(files) - 1} supporting note(s)" if len(files) > 1 else ""
    return NoteWrite(files=files, message=f"Add {note.type} note: {note.id}{extra}")


async def _unresolved_links(note: Note, landing: list[Note], notes_dir: Path) -> list[str]:
    """`note`'s link targets that no note defines, neither on disk nor in this write.

    Uses `kg.graph.dangling_links`, the one definition, over the cached corpus. `landing` is every
    note this write puts on disk, so a subject citing a dependency written beside it resolves.
    External ids resolve in a store and are not dangling.
    """
    existing = await note_store(notes_dir).notes()
    reported = await asyncio.to_thread(dangling_links, [*existing, *landing])
    # Deduplicated: on a re-record the subject is in both the corpus and `landing`.
    return list(dict.fromkeys(target for source, target in reported if source == note.id))


def count_notes_recorded(outcome: WriteOutcome | PutOutcome) -> None:
    """Book what `outcome` put in the graph on `chemclaw_notes_recorded_total`.

    Counted after the writer returns, so the number means "reached the graph", not "attempted". Also
    called by `cli/backfill_corpus` for a batch's final `flush()`, which `record_note` never sees.
    """
    if outcome.notes:
        record_metric(lambda m: m.increment("chemclaw_notes_recorded_total", outcome.notes))


async def _digests_before_retiring(superseded: list[Note], notes_dir: Path) -> dict[str, str]:
    """The digest of each note a retirement closes, as the store being read holds it now.

    A retirement copy is its note plus a sentence, made from a read some moments ago. Postgres
    keeps the sentence only on the note the copy was made from, so the unit names that note by its
    digest, taken before the write: for the tree's own replacement of the file it is the note being
    replaced, and for a Postgres-only write it is Postgres's.
    """
    held = await note_store(notes_dir).get_many([retired.id for retired in superseded])
    digests: dict[str, str] = {}
    for note_id, current in held.items():
        if current is not None:
            with contextlib.suppress(NoteError):
                digests[note_id] = content_sha(canonical_note(current))
    return digests


async def _put_in_postgres(unit: NoteUnit) -> PutOutcome:
    """Write `unit` to the Postgres store in one transaction."""
    return await postgres_note_store().put(unit)


async def _mirror_in_postgres(unit: NoteUnit) -> None:
    """Write `unit` to Postgres after the tree has it; a failure is loud and never the caller's.

    The chemist's note is already in the authoritative tree, so failing their call would report a
    loss that did not happen. The failure is logged with the note id, counted on
    `chemclaw_kg_dual_write_failed_total`, and found again by the nightly comparison, which
    `--repair` fixes; recording the same note again repairs it too, since a `put` is idempotent.
    A cancellation during the write is counted and logged the same way, at WARNING, and then
    re-raised: the note is in the tree whatever happened to the task that wrote it.
    """
    try:
        await _put_in_postgres(unit)
    except BaseException as exc:
        cancelled = not isinstance(exc, Exception)
        log_event(
            log,
            "kg.dual_write.failed",
            "note %s is in the tree but its Postgres copy could not be written (%s: %s); the "
            "stores differ until `kg-validate --compare --repair` or a repeat of the write",
            unit.subject.id,
            type(exc).__name__,
            exc,
            level=logging.WARNING if cancelled else logging.ERROR,
            exc_info=not cancelled,
            note_id=unit.subject.id,
        )
        record_metric(lambda m: m.increment("chemclaw_kg_dual_write_failed_total"))
        if cancelled:
            raise


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
        The writer's reference for what landed: a commit, or the unchanged tree (under
        `kg_store=postgres`, `kg_notes:<id>@<revision>`). A note whose `[[wikilinks]]` name ids
        nothing defines still lands, and logs a WARNING naming them.
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
    # rather than a refusal, so a typo'd citation does not lose the observation. The store reads
    # the corpus off the event loop.
    unresolved = await _unresolved_links(
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
    retired_from = (
        await _digests_before_retiring(superseded, Path(settings.note_repo_dir) / directory)
        if superseded and settings.kg_store != "git"
        else None
    )
    if settings.kg_store == "postgres":
        stored = await _put_in_postgres(_unit(note, dependencies, superseded, retired_from))
        count_notes_recorded(stored)
        return f"kg_notes:{note.id}@{stored.revisions[note.id]}"
    outcome = await writer.write(_build_write(note, directory, dependencies, superseded))
    # Booked before the mirror is awaited: the note is in the graph now, whatever the mirror does.
    count_notes_recorded(outcome)
    if settings.kg_store == "dual":
        await _mirror_in_postgres(_unit(note, dependencies, superseded, retired_from))
    return outcome.reference
