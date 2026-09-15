"""A backfill lands many notes in one commit; the conversational path still lands one at a time.

**The two paths want different write shapes, and until now they had one.** One commit and one push
per note is what bounds a backfill — measured here against a real bare remote: **140.9 ms per note**
unbatched against **15.8 ms** at ten to a commit and **4.8 ms** at fifty, a 29.4x difference at the
shipped default. `D-2026-09-13-the-lock-is-not-the-bound-the-commit-is` measured the same curve at a
10,000-note corpus (327.3 / 31.6 / 8.5 ms) and declined batching for the *conversational* path on
the product rather than the cost: a queued note is one a chemist cannot read yet, which is what
deleting the PR-gate bought. That argument does not reach an operator command over a directory of
existing documents, and this file is where the split is held.

Real git against a real bare remote, because what is being asserted is a commit count, and a fake
writer that counted calls would assert the wrapper's arithmetic rather than git's.
"""

import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from chemclaw.core.config import settings
from chemclaw.kg.git_writer import BatchingNoteWriter, GitNoteWriter
from chemclaw.kg.note import Note
from chemclaw.kg.record import record_note


def _run(*args: str, cwd: Path | None = None) -> str:
    """One git command, failing loudly — a fixture that half-built would fail as a finding."""
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _notes_repo(root: Path) -> Path:
    """A bare remote and a clone of it with an empty `knowledge/`, on `main`."""
    remote, clone = root / "remote.git", root / "clone"
    _run("git", "init", "--bare", "-q", "-b", "main", str(remote))
    _run("git", "clone", "-q", str(remote), str(clone))
    _run("git", "-C", str(clone), "config", "user.email", "backfill@example.test")
    _run("git", "-C", str(clone), "config", "user.name", "backfill")
    (clone / "knowledge").mkdir()
    (clone / "knowledge" / ".keep").write_text("", encoding="utf-8")
    _run("git", "-C", str(clone), "add", "-A")
    _run("git", "-C", str(clone), "commit", "-q", "-m", "seed")
    _run("git", "-C", str(clone), "branch", "-M", "main")
    _run("git", "-C", str(clone), "push", "-q", "origin", "HEAD:refs/heads/main")
    return clone


def _note(index: int) -> Note:
    """One backfilled document's note."""
    return Note(
        id=f"playbook-batch-{index:03d}",
        type="playbook",
        created_by="agent",
        body=f"Backfilled document {index}.",
    )


def _commits(clone: Path) -> int:
    """Commits on `main`, so a batch's saving is counted in the thing it actually saves."""
    return int(_run("git", "-C", str(clone), "rev-list", "--count", "HEAD"))


def test_a_batch_of_notes_lands_in_one_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six notes at a batch size of three are two commits, not six — and every note is on disk.

    Both halves matter. The commit count is the saving; the files are the thing that must not be
    traded for it, and a batching writer that dropped one would look exactly like a fast one.
    """
    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    before = _commits(clone)

    async def _run_backfill() -> None:
        writer = BatchingNoteWriter(inner, batch_size=3)
        for index in range(6):
            await record_note(_note(index), writer)
        await writer.flush()

    asyncio.run(_run_backfill())

    assert _commits(clone) - before == 2, "six notes at three to a commit must be two commits"
    written = sorted(p.stem for p in (clone / settings.knowledge_dir).rglob("*.md"))
    assert written == [f"playbook-batch-{i:03d}" for i in range(6)]


def test_an_unflushed_batch_is_not_on_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial batch is pending, which is why `flush()` is the caller's responsibility.

    This is the property that makes batching wrong for the conversational path even leaving the
    product argument aside: a note the model just wrote would not be there to read.
    """
    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    before = _commits(clone)

    async def _partial() -> str:
        writer = BatchingNoteWriter(inner, batch_size=10)
        reference = await record_note(_note(0), writer)
        return reference

    reference = asyncio.run(_partial())

    assert reference == "", "a pending note has no commit to name yet"
    assert _commits(clone) == before
    assert not list((clone / settings.knowledge_dir).rglob("playbook-batch-*.md"))


def test_flushing_nothing_commits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty backfill must not mint an empty commit — the idempotent no-op, at batch scale."""
    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    before = _commits(clone)

    outcome = asyncio.run(BatchingNoteWriter(inner, batch_size=5).flush())

    assert outcome.written is False and _commits(clone) == before


def test_a_batch_size_that_batches_nothing_is_refused() -> None:
    """`batch_size=1` is the unbatched writer wearing a wrapper, and reads as a configured mode.

    Refused rather than allowed-and-equivalent: a deployment that set it to 1 would be paying the
    pending-reference cost — `record_note` returning `""` — for no saving at all.
    """
    inner = GitNoteWriter(repo_dir=".", base_branch="main", remote="origin")
    for size in (0, 1):
        with pytest.raises(ValueError, match="does not batch anything"):
            BatchingNoteWriter(inner, batch_size=size)


def test_the_shipped_backfill_batches_and_nothing_else_does() -> None:
    """The split, asserted where it can be: exactly one module wraps the writer this way.

    A `BatchingNoteWriter` on the conversational path would be the thing
    `D-2026-09-13-the-lock-is-not-the-bound-the-commit-is` declined, arriving by import rather than
    by decision — and it would be invisible, because every test of that path injects its own writer.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    users = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "BatchingNoteWriter(" in path.read_text(encoding="utf-8")
    )
    assert users == ["cli/backfill_corpus.py"], (
        f"{users} construct a BatchingNoteWriter. Only the backfill command may: a batch is a "
        "queue, and a queued note is one a chemist cannot read yet."
    )


def test_every_note_in_a_batch_is_counted_once_rather_than_once_per_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chemclaw_notes_recorded_total` means "a note reached the graph", and batching broke that.

    `kg/record.record_note` gates the counter on `outcome.written` and says so in as many words —
    "the number means 'a note reached the graph' rather than 'we tried'". `BatchingNoteWriter.write`
    returned `written=False` for every held note and `True` only for the one that filled a batch, so
    at the shipped batch size of 50 a 10,000-note backfill moved the counter about 200 times; and
    the notes landed by the trailing `flush()` never reach `record_note` at all, so they moved it
    zero times.

    Driven against the real metric over a real remote, at a batch size that makes the two readings
    differ by a factor of three.
    """
    from chemclaw.core.metrics import METRICS

    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")

    before = METRICS.value("chemclaw_notes_recorded_total")

    async def _run_backfill() -> None:
        writer = BatchingNoteWriter(inner, batch_size=3)
        for index in range(7):
            await record_note(_note(index), writer)
        await writer.flush()

    asyncio.run(_run_backfill())

    assert _commits(clone) - 1 == 3, "seven at three to a commit is two full batches and a tail"
    assert METRICS.value("chemclaw_notes_recorded_total") - before == 7, (
        "the counter must count notes, not commits: it read 2 for these seven notes, because five "
        "were held and the tail flushed outside record_note entirely"
    )


def test_a_backfill_that_dies_mid_run_still_commits_what_it_already_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trailing flush is in a `finally`, because the pending batch is already reported written.

    `cli/backfill_corpus` logs each note as written "(pending a batch)" and increments its own
    `written` total *before* the batch commits, so a failure the loop's own `except
    (AttachmentError, OSError)` does not catch — a git error, a psycopg error, a `KeyboardInterrupt`
    on a long run — used to discard up to `batch_size - 1` notes that the operator had already been
    told were written. An operator running this over a decade of documents cannot recover from a
    silent tail.

    Driven through the CLI's own `backfill` with a reader that raises a type it does not catch, so
    the assertion is about the real control flow rather than `BatchingNoteWriter` in isolation.
    """
    import chemclaw.cli.backfill_corpus as module

    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    monkeypatch.setattr(settings, "backfill_commit_batch_size", 50)
    documents = tmp_path / "docs"
    documents.mkdir()
    for index in range(4):
        (documents / f"doc-{index}.txt").write_text(f"document {index}\n", encoding="utf-8")
    (documents / "zz-explodes.txt").write_text("boom\n", encoding="utf-8")

    real = module.note_for_document

    def _explode_on_the_last(path: Path, *args: object, **kwargs: object) -> Note:
        if path.name == "zz-explodes.txt":
            raise RuntimeError("the reader failed in a way the loop does not catch")
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "note_for_document", _explode_on_the_last)

    with pytest.raises(RuntimeError):
        asyncio.run(module.backfill(documents, tags=[], dry_run=False))

    landed = sorted(p.stem for p in (clone / settings.knowledge_dir).rglob("*.md"))
    assert len(landed) == 4, (
        f"the four notes already counted as written were dropped with the batch: {landed}"
    )


def test_a_dependency_in_a_batch_does_not_overwrite_a_subject_written_earlier_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching must reproduce the sequence it replaces, and it did not.

    `GitNoteWriter._write_and_commit` resolves every path and evaluates `if not file.overwrite and
    note_path.exists()` in **one plan pass before any byte is written** — its own comment says
    "every path is resolved and checked before any byte is written" — so merging N writes evaluates
    all of them against the *pre-batch* tree, where N commits would each have seen the previous one.
    The class docstring claimed the opposite ("applying them in order is exactly the sequence the
    unbatched path would apply").

    Driven both ways against a real remote: a subject note written first, then named as a stale
    dependency by a later note in the same batch. Unbatched the subject's own body survives;
    batched, before the fix, the dependency's copy won.
    """
    from chemclaw.kg.record import NoteFile, NoteWrite

    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    shared = f"{settings.knowledge_dir}/playbook-shared.md"

    async def _run_batch() -> None:
        writer = BatchingNoteWriter(inner, batch_size=10)
        await writer.write(
            NoteWrite(
                files=[NoteFile(path=shared, content="THE REAL SUBJECT BODY.\n", overwrite=True)],
                message="the subject",
            )
        )
        await writer.write(
            NoteWrite(
                files=[
                    NoteFile(
                        path=shared, content="a stale dependency rendering.\n", overwrite=False
                    )
                ],
                message="a later note's dependency copy",
            )
        )
        await writer.flush()

    asyncio.run(_run_batch())

    body = (clone / shared).read_text(encoding="utf-8")
    assert body == "THE REAL SUBJECT BODY.\n", (
        "a do-not-clobber dependency copy overwrote the subject note written earlier in the same "
        f"batch, which no sequence of unbatched writes would do: {body!r}"
    )


def test_a_batch_whose_commit_fails_counts_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse of the defect the counter fix closed, and the worse one of the two.

    `chemclaw_notes_recorded_total` is declared as "Notes written into the knowledge graph" and
    `record_note` gates it "so the number means 'a note reached the graph' rather than 'we tried'".
    The first repair had `BatchingNoteWriter.write` return `written=True` at *accept* time — driven
    with an inner writer that raises on commit, nine accepted notes moved the counter by 9 with
    **zero** notes in git. Counting where the answer exists (after the inner write returns) is what
    this holds.

    Both arms: the counter does not move, and the failure is not swallowed into a success.
    """
    from chemclaw.core.metrics import METRICS

    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))

    class _Refusing:
        """An inner writer that accepts nothing — a push rejection, an auth failure, a hook."""

        async def write(self, write: object) -> object:
            raise RuntimeError("git push rejected")

    before = METRICS.value("chemclaw_notes_recorded_total")

    async def _run_backfill() -> None:
        writer = BatchingNoteWriter(cast(Any, _Refusing()), batch_size=10)
        for index in range(4):
            await record_note(_note(index), writer)
        await writer.flush()

    with pytest.raises(RuntimeError, match="git push rejected"):
        asyncio.run(_run_backfill())

    assert METRICS.value("chemclaw_notes_recorded_total") == before, (
        "notes were counted as having reached the knowledge graph while the commit that would "
        "have put them there failed"
    )


def test_a_flush_that_fails_after_a_complete_loop_fails_the_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that reports `wrote N note(s)` and exits 0 with nothing in git is the worst outcome.

    The trailing flush was first repaired into a `finally` with a blanket `except`, which produced
    exactly that: driven through the real CLI with a failing writer, the loop completed, the flush
    raised, the exception was logged and swallowed, and `main` printed `wrote 4 note(s)` and
    returned **0**. Pre-fix that case at least exited non-zero, so the repair was strictly worse on
    the *common* failure — a push rejection, an auth failure, a pre-commit hook.

    The two exits are separated now: a flush after a completed loop is the last thing that can
    fail and its failure is the run's failure. `test_a_backfill_that_dies_mid_run_still_commits_
    what_it_already_counted` holds the other exit, where the flush is best-effort because the
    original cause is the one an operator needs.
    """
    import chemclaw.cli.backfill_corpus as module

    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    monkeypatch.setattr(settings, "backfill_commit_batch_size", 50)
    documents = tmp_path / "docs"
    documents.mkdir()
    for index in range(4):
        (documents / f"doc-{index}.txt").write_text(f"document {index}\n", encoding="utf-8")

    class _Refusing:
        async def write(self, write: object) -> object:
            raise RuntimeError("git push rejected")

    monkeypatch.setattr(module, "default_writer", lambda: cast(Any, _Refusing()))

    with pytest.raises(RuntimeError, match="git push rejected"):
        asyncio.run(module.backfill(documents, tags=[], dry_run=False))
