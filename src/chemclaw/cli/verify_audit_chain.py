"""Verify the tamper-evident hash chain over the GxP audit trail (plan F10-G1).

Walks `audit_events` in insertion order and checks three invariants:
1. `row_hash == chain_hash(prev_hash, event)` — the row's own audited fields are intact;
2. `prev_hash` equals the previous chained row's `row_hash` — no interior row was deleted or
   reordered;
3. the first chained row's `prev_hash` is empty — the genesis anchor. Without it, deleting a
   *leading* run of rows would leave a still-self-consistent chain (the new first row's link check
   is otherwise skipped), so this catches prefix truncation.

Any failure means the append-only trail was altered after the fact. Run as
`python -m chemclaw.cli.verify_audit_chain` (`make audit-verify`); it prints each problem and exits
non-zero if the chain is broken, so it can gate a compliance check in CI or an audit.

Known limit: deleting a *trailing* run of rows (tip truncation) cannot be detected from the chain
alone — the remaining rows still link cleanly and nothing records how many rows there should be.
Catching that needs an external append-count/max-id anchor recorded out-of-band; it is deferred (see
docs/planning/DEFERRED.md). The chain detects modification, reordering, interior deletion, and
prefix truncation.

The pure `check_chain` is separated from the database fetch so the invariants are unit-testable
offline over synthetic rows. Rows written before the chain migration (empty `row_hash`) are treated
as pre-chain and skipped until the first chained row (see `infra/sql/011_audit_hash_chain.sql`).
"""

import asyncio
from collections.abc import Iterable
from typing import NamedTuple

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import CHAIN_VERSION, chain_hash
from chemclaw.core.config import settings
from chemclaw.core.db import connection


class ChainRow(NamedTuple):
    """One `audit_events` row as the verifier reads it: its id, chain fields, and audited event.

    `chain_version` is which field set this row's `row_hash` covers
    (D-2026-07-31-the-audit-chain-is-versioned). It travels with the
    row rather than being assumed, because the audited record grows: hashing an old row under the
    current shape would report the whole trail as tampered with the day a field is added.
    """

    id: int
    prev_hash: str
    row_hash: str
    event: AuditEvent
    chain_version: int = CHAIN_VERSION


class ChainCheck:
    """A resumable fold over the chain, so verification can page the one table nothing may prune.

    The invariant is unchanged and `check_chain` is still the surface the tests drive; this exists
    only because the fold now has to survive a page boundary. `verify_chain` used to `fetchall()`
    the entire table — and that table is `audit_events`, the one `chemclaw.durable.retention`
    explicitly refuses to prune, because deleting from a hash chain is indistinguishable from the
    tampering the chain detects. So the one table guaranteed to grow forever was read whole into
    the shared background worker: the scheduled integrity check was on a path to OOM it, or to trip
    the statement timeout first and quietly stop verifying anything at all (DARK-6).
    """

    def __init__(self) -> None:
        """Start before the genesis row, with nothing found."""
        self.problems: list[str] = []
        # None means "no chained row seen yet", which is what makes the genesis check work: the
        # first row with a `row_hash` must carry an empty `prev_hash`. Carried across `feed` calls,
        # so paging is invisible to the invariant.
        self._expected_prev: str | None = None

    def feed(self, rows: Iterable[ChainRow]) -> None:
        """Check one contiguous, ascending run of rows, carrying the link across the call."""
        for row in rows:
            if not row.row_hash and self._expected_prev is None:
                continue  # pre-chain legacy row, before the chain begins
            if self._expected_prev is None:
                # The genesis row: the writer sets its prev_hash to "" (no tip to chain to). A
                # non-empty value means the true first row(s) were removed — prefix truncation the
                # per-row link check below cannot see, because there is no predecessor to compare
                # this row against.
                if row.prev_hash != "":
                    self.problems.append(
                        f"audit row {row.id}: broken genesis — the first chained row links to a "
                        "missing predecessor (a leading row was deleted)"
                    )
            elif row.prev_hash != self._expected_prev:
                self.problems.append(
                    f"audit row {row.id}: broken link — prev_hash does not match the previous row "
                    "(a row was deleted, inserted, or reordered)"
                )
            if chain_hash(row.prev_hash, row.event, version=row.chain_version) != row.row_hash:
                self.problems.append(
                    f"audit row {row.id}: content tampered — row_hash does not match its audited "
                    "fields"
                )
            self._expected_prev = row.row_hash


def check_chain(rows: Iterable[ChainRow]) -> list[str]:
    """Return human-readable chain problems in `rows` (empty if the chain is intact).

    `rows` must be in ascending insertion order. A leading run of rows with an empty `row_hash`
    (written before the chain migration) is skipped; verification begins at the first chained row,
    whose `prev_hash` must be empty (the genesis anchor — a non-empty one means a leading run was
    deleted). Every row after it must both hash correctly and link to its predecessor.
    """
    check = ChainCheck()
    check.feed(rows)
    return check.problems


# Paged by id rather than read whole: `audit_events` is the table retention refuses to prune, so
# it is the one table with no upper bound on its size. `id > %s` is the cursor — a `BIGSERIAL` is
# monotonic, so "everything after the last row I checked" is one indexed comparison and cannot skip
# or repeat a row the way an offset can. The chain fold is order-dependent, which is exactly why the
# cursor must be the primary key and the order must stay `ASC`.
_SELECT_PAGE = """
    SELECT id, correlation_id, session_id, purpose, actor, tool, arguments, outcome, detail,
           latency_ms, revision, prev_hash, row_hash, chain_version
    FROM audit_events
    WHERE id > %s
    ORDER BY id ASC
    LIMIT %s
"""


def _chain_row(record: tuple[object, ...]) -> ChainRow:
    """Build one `ChainRow` from a `_SELECT_PAGE` row, in its column order."""
    return ChainRow(
        id=int(str(record[0])),
        prev_hash=str(record[11]),
        row_hash=str(record[12]),
        event=AuditEvent(
            correlation_id=str(record[1]),
            session_id=str(record[2]),
            purpose=str(record[3]),
            actor=str(record[4]),
            tool=str(record[5]),
            arguments=str(record[6]),
            outcome=str(record[7]),
            detail=str(record[8]),
            latency_ms=float(str(record[9])),
            revision=str(record[10]),
        ),
        chain_version=int(str(record[13])),
    )


async def verify_chain(dsn: str | None = None) -> list[str]:
    """Walk the audit trail in pages and check its hash chain; return the problems found.

    One connection for the whole walk, one page in memory at a time. The page size is
    `audit_verify_page_rows`; the fold carries the link across pages, so paging is invisible to the
    invariant and a trail of any length verifies in bounded memory.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    check = ChainCheck()
    after = 0
    page_size = settings.audit_verify_page_rows
    async with connection(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        while True:
            cursor = await conn.execute(_SELECT_PAGE, (after, page_size))
            records = await cursor.fetchall()
            if not records:
                break
            check.feed(_chain_row(record) for record in records)
            after = int(str(records[-1][0]))
            if len(records) < page_size:
                break
    return check.problems


def main() -> int:
    """CLI entry point: verify the audit chain; print problems; return the exit code."""
    problems = asyncio.run(verify_chain())
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) — the audit trail hash chain is BROKEN")
        return 1
    print("OK: the audit trail hash chain is intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
