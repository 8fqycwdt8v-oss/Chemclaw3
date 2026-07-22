"""Verify the tamper-evident hash chain over the GxP audit trail (plan F10-G1).

Walks `audit_events` in insertion order and checks two invariants per row:
1. `row_hash == chain_hash(prev_hash, event)` — the row's own audited fields are intact;
2. `prev_hash` equals the previous chained row's `row_hash` — no row was deleted or reordered.

Either failure means the append-only trail was altered after the fact. Run as
`python -m scripts.verify_audit_chain` (`make audit-verify`); it prints each problem and exits
non-zero if the chain is broken, so it can gate a compliance check in CI or an audit.

The pure `check_chain` is separated from the database fetch so the invariants are unit-testable
offline over synthetic rows. Rows written before the chain migration (empty `row_hash`) are treated
as pre-chain and skipped until the first chained row (see `infra/sql/011_audit_hash_chain.sql`).
"""

import asyncio
from collections.abc import Iterable
from typing import NamedTuple

from agents.audit import AuditEvent
from agents.audit_store import chain_hash
from chemclaw.config import settings
from chemclaw.db import connect


class ChainRow(NamedTuple):
    """One `audit_events` row as the verifier reads it: its id, chain fields, and audited event."""

    id: int
    prev_hash: str
    row_hash: str
    event: AuditEvent


def check_chain(rows: Iterable[ChainRow]) -> list[str]:
    """Return human-readable chain problems in `rows` (empty if the chain is intact).

    `rows` must be in ascending insertion order. A leading run of rows with an empty `row_hash`
    (written before the chain migration) is skipped; verification begins at the first chained row
    and every row after it must both hash correctly and link to its predecessor.
    """
    problems: list[str] = []
    expected_prev: str | None = None
    for row in rows:
        if not row.row_hash and expected_prev is None:
            continue  # pre-chain legacy row, before the chain begins
        if expected_prev is not None and row.prev_hash != expected_prev:
            problems.append(
                f"audit row {row.id}: broken link — prev_hash does not match the previous row "
                "(a row was deleted, inserted, or reordered)"
            )
        if chain_hash(row.prev_hash, row.event) != row.row_hash:
            problems.append(
                f"audit row {row.id}: content tampered — row_hash does not match its audited fields"
            )
        expected_prev = row.row_hash
    return problems


_SELECT_ALL = """
    SELECT id, correlation_id, actor, tool, arguments, outcome, detail, latency_ms,
           prev_hash, row_hash
    FROM audit_events
    ORDER BY id ASC
"""


async def verify_chain(dsn: str | None = None) -> list[str]:
    """Fetch the whole audit trail and check its hash chain; return the problems found."""
    target = dsn if dsn is not None else settings.postgres_dsn
    rows: list[ChainRow] = []
    async with await connect(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_SELECT_ALL)
        for record in await cursor.fetchall():
            (rid, cid, actor, tool, args, outcome, detail, latency, prev_hash, row_hash) = record
            rows.append(
                ChainRow(
                    id=rid,
                    prev_hash=prev_hash,
                    row_hash=row_hash,
                    event=AuditEvent(
                        correlation_id=cid,
                        actor=actor,
                        tool=tool,
                        arguments=args,
                        outcome=outcome,
                        detail=detail,
                        latency_ms=latency,
                    ),
                )
            )
    return check_chain(rows)


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
