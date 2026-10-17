"""The erasure ledger: one line per applied erasure, in a file a database restore cannot rewind.

A restore discards every erasure committed after its recovery point, and a record of them kept in
the same database goes with them. So each applied erasure appends one JSON line to
`erasure_ledger_path`, on a volume backed up apart from the database, and after a restore every
line stamped after the recovery point is re-applied. A line holds what re-applying needs and
nothing more: the actor id, the instant it committed, and its scope — route `actor` (the
conversation tier of every spelling of that id, `agent/leaver.erase_actor`) or route `finish` (an
interrupted erasure's orphaned sessions, by id, `agent/leaver.finish_erasure`), in one tenant.

Invariants: an append is written under an exclusive POSIX record lock (`lockf`, which NFS honours
where `O_APPEND` alone is not atomic), then the file and its directory are fsynced before it
returns; a line that does not parse stops a read by line number, never skipped.
"""

import fcntl
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

#: Group-writable, so every pod of a release (one `fsGroup`) can append; nobody else may read it.
_FILE_MODE = 0o660

_ROUTES = ("actor", "finish")
_KEYS = {"actor", "at", "scope"}


class LedgerError(ChemclawError):
    """The ledger cannot be written or read, or no posture for keeping it was stated."""


@dataclass(frozen=True)
class ErasureRecord:
    """One applied erasure, as much of it as a re-application needs.

    `actor` is the id given to the actor route and `None` for a finish, which names `sessions`
    instead. `at` is no earlier than the commit, on the session database's clock.
    """

    at: datetime
    tenant: str
    actor: str | None = None
    sessions: tuple[str, ...] = ()

    @property
    def route(self) -> str:
        """`finish` for an erasure of orphaned sessions by id, else `actor`."""
        return "finish" if self.sessions else "actor"

    def line(self) -> str:
        """The record as one ledger line, without its newline."""
        scope: dict[str, object] = {"route": self.route, "tenant": self.tenant}
        if self.sessions:
            scope["sessions"] = list(self.sessions)
        return json.dumps(
            {"actor": self.actor, "at": self.at.astimezone(UTC).isoformat(), "scope": scope},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def parse(cls, text: str) -> "ErasureRecord":
        """The record one ledger line holds.

        Raises:
            ValueError: the line is not a record this module writes.
        """
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != _KEYS:
            raise ValueError(f"expected exactly the keys {sorted(_KEYS)}")
        scope, actor = data["scope"], data["actor"]
        if not isinstance(scope, dict) or scope.get("route") not in _ROUTES:
            raise ValueError(f"scope.route must be one of {_ROUTES}")
        tenant, sessions = scope.get("tenant"), scope.get("sessions", [])
        if not isinstance(tenant, str) or not tenant:
            raise ValueError("scope.tenant must be a tenant id")
        if scope["route"] == "actor" and not (isinstance(actor, str) and actor.strip()):
            raise ValueError("an actor record must name the actor")
        if scope["route"] == "finish" and not (
            actor is None
            and isinstance(sessions, list)
            and sessions
            and all(isinstance(s, str) for s in sessions)
        ):
            raise ValueError("a finish record names its sessions and no actor")
        return cls(
            at=parse_instant(str(data["at"])), tenant=tenant, actor=actor, sessions=tuple(sessions)
        )


def parse_instant(text: str) -> datetime:
    """An instant as an operator or this module writes it: ISO 8601, or `2026-10-14 09:41:00+00`.

    Raises:
        LedgerError: it does not parse, or it carries no UTC offset, which would leave the instant
            to whichever zone reads it.
    """
    try:
        instant = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise LedgerError(f"{text!r} is not an ISO 8601 instant") from exc
    if instant.utcoffset() is None:
        raise LedgerError(
            f"{text!r} names no UTC offset; write the recovery point as Postgres does, "
            "e.g. '2026-10-14 09:41:00+00'"
        )
    return instant


def ledger_for_apply() -> Path | None:
    """The ledger an applied erasure appends to, or `None` when the operator keeps the record.

    Asked before anything is erased: an erasure a restore cannot find is the defect this module
    exists for. The ledger is opened (and created) here, so an unwritable path refuses up front.

    Raises:
        LedgerError: no ledger is configured and nobody stated that the record is kept by hand, or
            the configured one cannot be appended to.
    """
    if not settings.erasure_ledger_path:
        if settings.erasure_ledger_kept_by_hand:
            return None
        raise LedgerError(
            "an applied erasure must be recorded where a database restore cannot rewind it, and "
            "CHEMCLAW_ERASURE_LEDGER_PATH is unset. Set it to a file on a volume backed up apart "
            "from the database, or set CHEMCLAW_ERASURE_LEDGER_KEPT_BY_HAND=true to state that you "
            "record every erasure yourself (each one then prints the line to record)"
        )
    path = Path(settings.erasure_ledger_path)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE))
        _fsync_directory(path.parent)
    except OSError as exc:
        raise LedgerError(
            f"the erasure ledger {path} cannot be appended to ({exc.strerror}); nothing was erased"
        ) from exc
    return path


def append(path: Path, record: ErasureRecord) -> None:
    """Append `record` as one line, durably, safe against another writer doing the same.

    The lock serializes writers across processes and hosts, so a line written in several `write`
    calls is still contiguous. A last line left without its newline (a writer killed mid-write) is
    closed first, so it cannot swallow this record into a line that does not parse.

    Raises:
        OSError: the line could not be written or made durable.
    """
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, _FILE_MODE)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            torn = size > 0 and os.pread(fd, 1, size - 1) != b"\n"
            data = (b"\n" if torn else b"") + record.line().encode() + b"\n"
            while data:
                data = data[os.write(fd, data) :]
            os.fsync(fd)
        finally:
            fcntl.lockf(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def read_records(path: Path) -> list[ErasureRecord]:
    """Every record in the ledger, in the order they were appended.

    A missing file is refused rather than read as empty: a replay in a pod without the ledger's
    volume would otherwise re-apply nothing and report success.

    Raises:
        LedgerError: the file is missing or unreadable, or a line is not a record (named by number).
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LedgerError(
            f"cannot read the erasure ledger {path} ({exc.strerror}). If no erasure was ever "
            "applied here, create it empty to say so"
        ) from exc
    records: list[ErasureRecord] = []
    for number, text in enumerate(lines, start=1):
        if not text.strip():
            continue
        try:
            records.append(ErasureRecord.parse(text))
        except (ValueError, LedgerError) as exc:
            raise LedgerError(
                f"{path} line {number} is not an erasure record ({exc}): {text[:200]!r}. A line "
                "cut short by a crash belongs to an erasure that committed: re-run that erasure (it "
                "is idempotent and records itself), then delete the line"
            ) from exc
    return records


def records_since(path: Path, since: datetime) -> list[ErasureRecord]:
    """The distinct records stamped after `since`, each at its first appearance.

    A repeat (the same actor or sessions in the same tenant) is dropped: re-applying an erasure
    twice removes nothing the first did not, and each replay appends records of its own.
    """
    distinct: dict[tuple[str | None, str, Sequence[str]], ErasureRecord] = {}
    for record in read_records(path):
        if record.at > since:
            distinct.setdefault((record.actor, record.tenant, record.sessions), record)
    return list(distinct.values())


def _fsync_directory(directory: Path) -> None:
    """Make a new file's directory entry durable, which fsyncing the file alone does not."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
