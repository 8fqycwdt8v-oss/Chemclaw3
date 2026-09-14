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

import ast
import asyncio
import subprocess
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.kg.git_writer import BatchingNoteWriter, GitNoteWriter
from chemclaw.kg.note import Note
from chemclaw.kg.record import count_notes_recorded, record_note


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


_BATCHER = "BatchingNoteWriter"


def _names_the_batcher(path: Path) -> bool:
    """Whether this module can reach `BatchingNoteWriter` at all, parsed rather than grepped.

    **The scan this replaces looked for the literal `BatchingNoteWriter(`**, which is one spelling
    of one way to construct it. Driven: a second module doing
    `__import__("chemclaw.kg.git_writer", fromlist=["x"]).BatchingNoteWriter(...)` left the check
    **green**, and `cls = BatchingNoteWriter` followed by `cls(...)` evades it the same way — a
    control that matches a comment rather than a construction.

    So the question asked is not "does this module call it" but "can this module *name* it", which
    is the property a caller cannot route around: to construct a class you must first bind it. Three
    arms, because there are three ways to bind one and all three are visible without running
    anything — an `import`, an attribute access on the module, and a string handed to `getattr`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == _BATCHER for a in node.names):
            return True
        if isinstance(node, ast.Attribute) and node.attr == _BATCHER:
            return True
        if isinstance(node, ast.Constant) and node.value == _BATCHER:
            return True
    return False


def test_the_shipped_backfill_batches_and_nothing_else_does() -> None:
    """The split, asserted where it can be: exactly one module can reach the batching writer.

    A `BatchingNoteWriter` on the conversational path would be the thing
    `D-2026-09-13-the-lock-is-not-the-bound-the-commit-is` declined, arriving by import rather than
    by decision — and it would be invisible, because every test of that path injects its own writer.

    `kg/git_writer.py` is absent from the list and does not need an exemption: it *defines* the
    class, which is a `ClassDef` rather than any of the three bindings above — so the allowlist
    stays one entry and says exactly what it means.

    What this still cannot see is a module that receives an already-constructed one as an argument.
    That is not the failure mode the rule is about: such a writer is the caller's decision, and the
    caller is in this scan.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    users = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*.py") if _names_the_batcher(path)
    )
    assert users == ["cli/backfill_corpus.py"], (
        f"{users} can reach a BatchingNoteWriter. Only the backfill command may: a batch is a "
        "queue, and a queued note is one a chemist cannot read yet."
    )


def test_the_counter_counts_notes_and_not_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`chemclaw_notes_recorded_total` must move by the notes, not by the commits carrying them.

    **Measured before this test existed: fifty notes moved it by 1.0.** `record_note` incremented
    on `outcome.written`, and under batching every note but the one that fills a batch returns
    `written=False` — so the counter became a count of *commits*, and the operator reading "how
    much has this backfill written" was reading a number fifty times too small at the shipped
    batch size. The whole point of the metric is that a write path failing every note cannot
    report healthy; a write path succeeding on fifty and reporting 1 fails the same sentence from
    the other end.

    Driven on real git against a real bare remote, like the rest of this file: the count has to be
    true of what landed, and a fake writer would assert the wrapper's arithmetic.
    """
    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    before_commits = _commits(clone)
    before_notes = METRICS.value("chemclaw_notes_recorded_total")

    async def _run_backfill() -> None:
        writer = BatchingNoteWriter(inner, batch_size=3)
        for index in range(7):
            await record_note(_note(index), writer)
        count_notes_recorded(await writer.flush())

    asyncio.run(_run_backfill())

    # Seven notes in three commits — two full batches and the flushed remainder. The two numbers
    # are asserted together because either alone is satisfiable by the defect: counting commits
    # gives 3, and dropping the batching gives 7 == 7 with the saving gone.
    assert _commits(clone) - before_commits == 3
    assert METRICS.value("chemclaw_notes_recorded_total") - before_notes == 7


def test_a_batch_that_changed_nothing_counts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, which a plain `len(batch)` would get wrong.

    Re-running a backfill over documents already in the corpus is the frequent, legitimate case,
    and the inner writer answers it with a no-op — nothing committed. A batch reporting its size
    regardless would turn "notes recorded" into "notes offered", which is the attempt-counting the
    metric was declared to avoid.
    """
    clone = _notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    inner = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")

    async def _run_backfill() -> float:
        for index in range(4):
            await record_note(_note(index), inner)
        before = METRICS.value("chemclaw_notes_recorded_total")
        writer = BatchingNoteWriter(inner, batch_size=4)
        for index in range(4):
            await record_note(_note(index), writer)
        count_notes_recorded(await writer.flush())
        return METRICS.value("chemclaw_notes_recorded_total") - before

    assert asyncio.run(_run_backfill()) == 0.0
