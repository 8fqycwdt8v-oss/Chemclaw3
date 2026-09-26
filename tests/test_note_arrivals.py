"""A note the model could not date still reaches a subscriber, on the day it arrived.

`durable/digest._is_new` read an absent `valid_from` as *open-ended* — correct about the fact and
wrong about the question a digest asks — so an undated note reached only a subscriber who had never
been told anything, and 34 of the shipped corpus's 41 notes are undated. `kg.graph.note_arrivals`
answers "when did this note reach the corpus" from the notes repository's own history: one
`git log` per process, then only the commits since the one it remembers (measured on a 10,000-note
corpus written one commit per note: ~2.9 s full, 78 ms for the last 100 commits).
"""

import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import chemclaw.durable.digest
from chemclaw.agent.subscriptions import Subscription
from chemclaw.core.config import settings
from chemclaw.durable.digest import _is_new
from chemclaw.kg import graph
from chemclaw.kg.graph import invalidate_cache, note_arrivals

#: A shipped note with no `valid_from` — the shape the row is about.
_UNDATED = Path(__file__).resolve().parents[1] / "knowledge" / "playbook" / "playbook-degassing.md"


def _git(repo: Path, *args: str, day: str = "2026-09-01", at: str = "10:00:00+00:00") -> None:
    """Run git in `repo` with both dates pinned, so an arrival is a date the test chose."""
    stamp = f"{day}T{at}"
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp, "PATH": "/usr/bin:/bin"},
    )


def _add(repo: Path, relative: str, day: str, text: str = "---\nid: x\n---\n") -> None:
    """Commit one note file on `day`."""
    path = repo / "knowledge" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A", day=day)
    _git(repo, "commit", "-qm", f"add {relative}", day=day)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty notes repository, and a cold arrival cache."""
    _git(tmp_path, "init", "-q")
    graph._ARRIVALS.clear()
    return tmp_path


def test_each_note_arrives_on_the_day_its_file_was_committed(repo: Path) -> None:
    """Keyed by note id, the file's stem — the one filename shape a note is written under."""
    _add(repo, "playbook/playbook-a.md", "2026-09-01")
    _add(repo, "compound/compound-b.md", "2026-09-03")
    assert note_arrivals(repo / "knowledge") == {
        "playbook-a": date(2026, 9, 1),
        "compound-b": date(2026, 9, 3),
    }


def test_a_later_call_scans_only_what_arrived_since(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost that makes it affordable hourly: after the first scan, a range, not all of history.

    And an edit is not an arrival: a note rewritten on a later day keeps the day it first came.
    """
    _add(repo, "playbook/playbook-a.md", "2026-09-01")
    notes = repo / "knowledge"
    assert note_arrivals(notes) == {"playbook-a": date(2026, 9, 1)}

    scanned: list[str | None] = []
    real = graph._added_since

    def _recording(notes_dir: Path, since: str | None) -> dict[str, date] | None:
        scanned.append(since)
        return real(notes_dir, since)

    monkeypatch.setattr(graph, "_added_since", _recording)
    _add(repo, "playbook/playbook-a.md", "2026-09-05", text="---\nid: x\n---\nedited\n")
    _add(repo, "report/report-c.md", "2026-09-06")
    assert note_arrivals(notes) == {
        "playbook-a": date(2026, 9, 1),
        "report-c": date(2026, 9, 6),
    }
    assert scanned and scanned[0] is not None, "the second call re-read the whole history"
    assert note_arrivals(notes) is note_arrivals(notes), "an unchanged HEAD scanned again"


def test_a_rewritten_history_is_read_again_whole(repo: Path) -> None:
    """A remembered commit that is no longer an ancestor cannot anchor a range, so it is unused."""
    _add(repo, "playbook/playbook-a.md", "2026-09-01")
    notes = repo / "knowledge"
    note_arrivals(notes)
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "rm", "-rq", "--cached", ".")
    shutil.rmtree(notes)
    _add(repo, "compound/compound-b.md", "2026-09-10")
    assert note_arrivals(notes) == {"compound-b": date(2026, 9, 10)}


def test_a_corpus_git_cannot_describe_has_no_arrivals(tmp_path: Path) -> None:
    """Not a work tree — a tarball deploy, every offline fixture — reads as no constraint."""
    graph._ARRIVALS.clear()
    assert note_arrivals(tmp_path) == {}


def test_an_undated_note_is_new_on_the_day_it_arrived_and_once() -> None:
    """The arrival stands in for the missing date, and the same-day id memory still applies."""
    watermark = datetime(2026, 9, 5, 9, tzinfo=UTC)

    def _sub(seen: list[str]) -> Subscription:
        return Subscription(
            id=1, owner="o", query="q", last_seen_at=watermark, last_seen_note_ids=seen
        )

    class _Undated:
        id = "playbook-a"
        valid_from = None

    note = _Undated()
    assert _is_new(note, _sub([]), date(2026, 9, 6)) is True  # type: ignore[arg-type]
    assert _is_new(note, _sub([]), date(2026, 9, 4)) is False  # type: ignore[arg-type]
    assert _is_new(note, _sub([]), date(2026, 9, 5)) is True  # type: ignore[arg-type]
    assert _is_new(note, _sub(["playbook-a"]), date(2026, 9, 5)) is False  # type: ignore[arg-type]
    assert _is_new(note, _sub([]), None) is False  # type: ignore[arg-type]


def test_a_digest_reports_an_undated_note_that_arrived_after_the_watermark(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end over a real committed corpus: the silence the row measured, now a delivery.

    Red before this change: the shipped playbook carries no `valid_from`, so a subscriber who had
    been told anything at all was never told about it.
    """
    assert "valid_from" not in _UNDATED.read_text(encoding="utf-8")
    target = repo / "knowledge" / "playbook" / _UNDATED.name
    target.parent.mkdir(parents=True)
    shutil.copy(_UNDATED, target)
    _git(repo, "add", "-A", day="2026-09-06")
    _git(repo, "commit", "-qm", "record", day="2026-09-06")
    monkeypatch.setattr(settings, "note_repo_dir", str(repo))
    invalidate_cache()

    term = _UNDATED.stem.split("-")[-1]
    told_before = Subscription(
        id=1,
        owner="o",
        query=term,
        last_seen_at=datetime(2026, 9, 5, 9, tzinfo=UTC),
        last_seen_note_ids=[],
    )
    items = chemclaw.durable.digest._match_corpus([told_before])
    assert [item.note_ids for item in items] == [[_UNDATED.stem]], (
        "an undated note committed after the subscriber's watermark was not reported"
    )


def test_an_arrival_is_dated_in_utc_whatever_the_committers_offset(repo: Path) -> None:
    """23:30 at -05:00 on the 5th is the 6th in UTC, which is the zone the watermark is read in.

    Dated in the committer's own offset it read as the 5th, a day before a watermark set early on
    the 6th, and the note was never reported; a positive offset delivered it twice.
    """
    path = repo / "knowledge" / "playbook" / "playbook-late.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: x\n---\n", encoding="utf-8")
    _git(repo, "add", "-A", day="2026-09-05", at="23:30:00-05:00")
    _git(repo, "commit", "-qm", "late", day="2026-09-05", at="23:30:00-05:00")
    assert note_arrivals(repo / "knowledge") == {"playbook-late": date(2026, 9, 6)}


@pytest.mark.parametrize("warm", [False, True], ids=["full-scan", "since-range"])
def test_a_note_merged_from_a_back_dated_branch_arrives_on_the_merge(
    repo: Path, warm: bool
) -> None:
    """A `--no-ff` merge is the day the note reached this branch, not the day it was written.

    Without `--first-parent -m` the file is attributed to its side-branch commit, so a note merged
    on 02-01 from a branch committed on 01-02 read as 01-02 — older than a subscriber's 01-15
    watermark, and so never reported. The `since..HEAD` range has the same defect, because the
    side-branch commits are in it with their old dates; both paths are driven.
    """
    _add(repo, "playbook/playbook-a.md", "2026-01-01")
    notes = repo / "knowledge"
    if warm:
        assert note_arrivals(notes) == {"playbook-a": date(2026, 1, 1)}
    _git(repo, "checkout", "-qb", "side", day="2026-01-02")
    _add(repo, "playbook/playbook-b.md", "2026-01-02")
    _git(repo, "checkout", "-q", "-", day="2026-01-10")
    _add(repo, "playbook/playbook-c.md", "2026-01-10")
    _git(repo, "merge", "--no-ff", "-qm", "merge side", "side", day="2026-02-01")
    invalidate_cache(notes)

    assert note_arrivals(notes)["playbook-b"] == date(2026, 2, 1)
