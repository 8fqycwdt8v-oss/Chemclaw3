"""The tamper-evident hash chain over the GxP audit trail (plan F10-G1).

Offline: `chain_hash` is deterministic and field-sensitive, and `check_chain` accepts an intact
chain while flagging a mutated field, a broken link (deletion/reorder), and a legacy pre-chain
prefix. Server-backed (skips offline): the real `PostgresAuditSink` writes a linked chain that
`verify_chain` confirms, and mutating a stored row makes it fail.
"""

import asyncio

from agents.audit import AuditEvent
from agents.audit_store import PostgresAuditSink, chain_hash
from chemclaw.config import settings
from scripts.verify_audit_chain import ChainRow, check_chain, verify_chain
from tests.pg import migrated_db_or_skip


def _event(tool: str, *, actor: str = "u-1", detail: str = "") -> AuditEvent:
    """A minimal audit event for chaining tests."""
    return AuditEvent(
        correlation_id="c-1",
        actor=actor,
        tool=tool,
        arguments="{}",
        outcome="ok",
        detail=detail,
        latency_ms=1.0,
    )


def _linked(events: list[AuditEvent]) -> list[ChainRow]:
    """Build a correctly-linked chain of rows from `events` (the writer's invariant, in-memory)."""
    rows: list[ChainRow] = []
    prev = ""
    for i, event in enumerate(events, start=1):
        row_hash = chain_hash(prev, event)
        rows.append(ChainRow(id=i, prev_hash=prev, row_hash=row_hash, event=event))
        prev = row_hash
    return rows


def test_chain_hash_is_deterministic_and_field_sensitive() -> None:
    """The same (prev, event) hashes identically; changing any audited field changes the hash."""
    event = _event("find_notes")
    assert chain_hash("abc", event) == chain_hash("abc", event)  # deterministic
    assert chain_hash("abc", event) != chain_hash("xyz", event)  # prev is part of the link
    assert chain_hash("abc", event) != chain_hash("abc", _event("find_notes", detail="x"))


def test_check_chain_accepts_an_intact_chain() -> None:
    """A correctly-linked chain reports no problems."""
    rows = _linked([_event("find_notes"), _event("submit_qm_job"), _event("expand_note")])
    assert check_chain(rows) == []


def test_check_chain_flags_a_mutated_row() -> None:
    """Altering a stored row's audited field (without re-hashing) is detected as tampering."""
    rows = _linked([_event("find_notes"), _event("submit_qm_job")])
    tampered = rows[1]._replace(event=_event("submit_qm_job", actor="attacker"))
    problems = check_chain([rows[0], tampered])
    assert any("tampered" in p for p in problems)


def test_check_chain_flags_a_deleted_row() -> None:
    """Dropping a middle row breaks the prev_hash link of the row that followed it."""
    rows = _linked([_event("a"), _event("b"), _event("c")])
    problems = check_chain([rows[0], rows[2]])  # row 2 removed
    assert any("broken link" in p for p in problems)


def test_check_chain_skips_a_legacy_pre_chain_prefix() -> None:
    """Rows written before the migration (empty row_hash) are skipped until the chain begins."""
    legacy = ChainRow(id=1, prev_hash="", row_hash="", event=_event("old"))
    chained = _linked([_event("new-1"), _event("new-2")])
    rows = [legacy, chained[0]._replace(id=2), chained[1]._replace(id=3)]
    assert check_chain(rows) == []


def test_postgres_sink_writes_a_verifiable_chain() -> None:
    """The real sink appends a linked chain `verify_chain` confirms; tampering breaks it."""

    async def _run() -> None:
        await migrated_db_or_skip()
        # Isolate this test's rows: verify_chain reads the whole table, so start from a clean one.
        import psycopg

        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE audit_events RESTART IDENTITY")
            await conn.commit()

        sink = PostgresAuditSink()
        await sink.record(_event("find_notes"))
        await sink.record(_event("submit_qm_job"))
        await sink.record(_event("expand_note"))

        assert await verify_chain() == []  # a freshly written chain verifies

        # Mutate one stored row's audited field without recomputing its hash → chain breaks.
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("UPDATE audit_events SET actor = 'attacker' WHERE id = 2")
            await conn.commit()
        assert await verify_chain() != []

    asyncio.run(_run())
