"""The erasure ledger: what a line holds, that appends are durable and serialized, how it is read.

No database: the ledger is a file, and its properties are a file's. The CLI that writes and replays
it against Postgres is tested in `tests/test_leaver.py`, and a real restore in
`tests/test_restore_drill.py`.
"""

import fcntl
import json
import multiprocessing
import os
import stat
import time
from datetime import UTC, datetime, timedelta, timezone
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from chemclaw.core import erasure_ledger
from chemclaw.core.config import settings
from chemclaw.core.erasure_ledger import ErasureRecord, LedgerError

_AT = datetime(2026, 10, 17, 9, 41, 0, 123456, tzinfo=UTC)


def _actor(actor: str = "oid-anna", at: datetime = _AT, tenant: str = "default") -> ErasureRecord:
    return ErasureRecord(at=at, tenant=tenant, actor=actor)


def test_a_line_holds_the_actor_the_commit_instant_and_the_scope_and_nothing_else(
    tmp_path: Path,
) -> None:
    """A record is what re-applying needs: no counts, no names beyond the id, no free text."""
    path = tmp_path / "erasures.jsonl"
    finish = ErasureRecord(at=_AT, tenant="acme", sessions=("s-1", "s-2"))
    erasure_ledger.append(path, _actor())
    erasure_ledger.append(path, finish)

    first, second = (json.loads(line) for line in path.read_text().splitlines())
    assert first == {
        "actor": "oid-anna",
        "at": "2026-10-17T09:41:00.123456+00:00",
        "scope": {"route": "actor", "tenant": "default"},
    }
    assert second == {
        "actor": None,
        "at": "2026-10-17T09:41:00.123456+00:00",
        "scope": {"route": "finish", "sessions": ["s-1", "s-2"], "tenant": "acme"},
    }
    assert erasure_ledger.read_records(path) == [_actor(), finish]


def test_an_instant_in_another_zone_is_written_in_utc_and_reads_back_as_the_same_instant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "erasures.jsonl"
    local = _AT.astimezone(timezone(timedelta(hours=2)))
    erasure_ledger.append(path, _actor(at=local))
    assert json.loads(path.read_text())["at"].endswith("+00:00")
    assert erasure_ledger.read_records(path)[0].at == _AT


def test_an_append_fsyncs_the_file_and_then_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that returned is on disk, and so is the directory entry of a ledger it created."""
    synced: list[bool] = []
    real_fsync = os.fsync

    def recording(fd: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording)
    erasure_ledger.append(tmp_path / "erasures.jsonl", _actor())
    assert synced == [False, True], "the file, then its directory"


def _hold_the_lock_mid_line(path: str, held: Event, release: Event) -> None:
    """Another writer: lock the ledger, write half a line, and finish it only when told."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o660)
    fcntl.lockf(fd, fcntl.LOCK_EX)
    os.write(fd, b'{"actor":"oid-first",')
    held.set()
    release.wait(30)
    os.write(
        fd, b'"at":"2026-10-17T09:00:00+00:00","scope":{"route":"actor","tenant":"default"}}\n'
    )
    os.fsync(fd)
    fcntl.lockf(fd, fcntl.LOCK_UN)
    os.close(fd)


def _append_second(path: str) -> None:
    erasure_ledger.append(Path(path), _actor("oid-second"))


def test_a_second_writer_waits_for_the_first_rather_than_landing_inside_its_line(
    tmp_path: Path,
) -> None:
    """Two erasures racing from two processes each get a whole line, in the order they locked.

    The first writer holds the lock between two halves of its line, which is the window in which an
    unlocked `O_APPEND` writer on NFS — or a writer whose line takes two `write` calls anywhere —
    lands inside it. The test asserts the second writer was actually held there.
    """
    path = tmp_path / "erasures.jsonl"
    context = multiprocessing.get_context("fork")
    held, release = context.Event(), context.Event()
    first = context.Process(target=_hold_the_lock_mid_line, args=(str(path), held, release))
    first.start()
    assert held.wait(10), "the first writer never took the lock"
    second = context.Process(target=_append_second, args=(str(path),))
    second.start()
    time.sleep(1.0)
    try:
        assert second.is_alive(), "the second writer did not wait for the lock"
        assert path.read_bytes() == b'{"actor":"oid-first",', "it wrote while the lock was held"
    finally:
        release.set()
        first.join(10)
        second.join(10)
    assert first.exitcode == 0 and second.exitcode == 0
    assert [r.actor for r in erasure_ledger.read_records(path)] == ["oid-first", "oid-second"]


def test_a_line_cut_short_by_a_crash_keeps_the_next_record_whole_and_stops_a_read_by_number(
    tmp_path: Path,
) -> None:
    """A torn line belongs to an erasure that committed, so it is named, never skipped."""
    path = tmp_path / "erasures.jsonl"
    path.write_bytes(b'{"actor":"oid-torn","at":"2026-10-1')
    erasure_ledger.append(path, _actor())
    lines = path.read_text().splitlines()
    assert lines[0] == '{"actor":"oid-torn","at":"2026-10-1'
    assert ErasureRecord.parse(lines[1]) == _actor()
    with pytest.raises(LedgerError, match=r"line 1 is not an erasure record.*re-run that erasure"):
        erasure_ledger.read_records(path)


@pytest.mark.parametrize(
    "line",
    [
        '{"actor":"oid-a","at":"2026-10-17T09:00:00+00:00","scope":{"route":"actor","tenant":"t"},'
        '"rows":3}',
        '{"actor":null,"at":"2026-10-17T09:00:00+00:00","scope":{"route":"actor","tenant":"t"}}',
        '{"actor":"oid-a","at":"2026-10-17T09:00:00+00:00","scope":{"route":"finish",'
        '"sessions":["s"],"tenant":"t"}}',
        '{"actor":null,"at":"2026-10-17T09:00:00+00:00","scope":{"route":"finish","tenant":"t"}}',
        '{"actor":"oid-a","at":"2026-10-17T09:00:00+00:00","scope":{"route":"purge","tenant":"t"}}',
        '{"actor":"oid-a","at":"2026-10-17T09:00:00","scope":{"route":"actor","tenant":"t"}}',
        '{"actor":"oid-a","at":"2026-10-17T09:00:00+00:00","scope":{"route":"actor"}}',
    ],
    ids=["extra-key", "actor-without-id", "finish-with-actor", "finish-without-sessions",
         "unknown-route", "naive-instant", "no-tenant"],
)  # fmt: skip
def test_a_line_that_is_not_exactly_a_record_is_refused(tmp_path: Path, line: str) -> None:
    path = tmp_path / "erasures.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(LedgerError, match="line 1 is not an erasure record"):
        erasure_ledger.read_records(path)


def test_a_missing_ledger_is_refused_rather_than_read_as_nothing_to_replay(
    tmp_path: Path,
) -> None:
    """A replay in a pod without the ledger's volume must not report success over nothing."""
    with pytest.raises(LedgerError, match="create it empty"):
        erasure_ledger.read_records(tmp_path / "absent.jsonl")
    (tmp_path / "empty.jsonl").touch()
    assert erasure_ledger.read_records(tmp_path / "empty.jsonl") == []


def test_a_replay_reads_the_records_strictly_after_the_point_once_each(tmp_path: Path) -> None:
    """The record at the point is inside the restore; a repeat of an erasure is applied once."""
    path = tmp_path / "erasures.jsonl"
    second = timedelta(seconds=1)
    anna_at_point = _actor("oid-anna", _AT)
    anna_after = _actor("oid-anna", _AT + second)
    ben = _actor("oid-ben", _AT + 2 * second)
    anna_in_acme = _actor("oid-anna", _AT + 3 * second, tenant="acme")
    anna_replayed = _actor("oid-anna", _AT + 4 * second)
    for record in (anna_at_point, anna_after, ben, anna_in_acme, anna_replayed):
        erasure_ledger.append(path, record)

    assert erasure_ledger.records_since(path, _AT) == [anna_after, ben, anna_in_acme]
    assert erasure_ledger.records_since(path, _AT + 4 * second) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-14 09:41:00+00", datetime(2026, 10, 14, 9, 41, tzinfo=UTC)),
        ("2026-10-14 09:41:00.5+00", datetime(2026, 10, 14, 9, 41, 0, 500000, tzinfo=UTC)),
        ("2026-10-14T09:41:00Z", datetime(2026, 10, 14, 9, 41, tzinfo=UTC)),
        ("2026-10-14T11:41:00+02:00", datetime(2026, 10, 14, 9, 41, tzinfo=UTC)),
    ],
)
def test_a_recovery_point_is_read_as_postgres_and_iso_8601_write_it(
    text: str, expected: datetime
) -> None:
    assert erasure_ledger.parse_instant(text) == expected


def test_a_recovery_point_without_an_offset_or_that_is_no_instant_is_refused() -> None:
    with pytest.raises(LedgerError, match="no UTC offset"):
        erasure_ledger.parse_instant("2026-10-14 09:41:00")
    with pytest.raises(LedgerError, match="not an ISO 8601 instant"):
        erasure_ledger.parse_instant("before lunch")


def test_an_applied_erasure_needs_a_ledger_or_the_stated_posture_of_keeping_it_by_hand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed, by name: the defect is an erasure a restore cannot find."""
    monkeypatch.setattr(settings, "erasure_ledger_path", "")
    monkeypatch.setattr(settings, "erasure_ledger_kept_by_hand", False)
    with pytest.raises(LedgerError) as refused:
        erasure_ledger.ledger_for_apply()
    assert "CHEMCLAW_ERASURE_LEDGER_PATH" in str(refused.value)
    assert "CHEMCLAW_ERASURE_LEDGER_KEPT_BY_HAND" in str(refused.value)

    monkeypatch.setattr(settings, "erasure_ledger_kept_by_hand", True)
    assert erasure_ledger.ledger_for_apply() is None

    monkeypatch.setattr(settings, "erasure_ledger_path", str(tmp_path / "no-volume" / "e.jsonl"))
    with pytest.raises(LedgerError, match="cannot be appended to.*nothing was erased"):
        erasure_ledger.ledger_for_apply()

    ledger = tmp_path / "erasures.jsonl"
    monkeypatch.setattr(settings, "erasure_ledger_path", str(ledger))
    assert erasure_ledger.ledger_for_apply() == ledger
    assert ledger.read_bytes() == b"", "opened for appending, and nothing written"
    assert stat.S_IMODE(ledger.stat().st_mode) & 0o007 == 0, "no access for anybody else"
