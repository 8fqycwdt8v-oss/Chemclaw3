"""The tamper-evident hash chain over the GxP audit trail (plan F10-G1).

Offline: `chain_hash` is deterministic and field-sensitive, and `check_chain` accepts an intact
chain while flagging a mutated field, a broken link (deletion/reorder), and a legacy pre-chain
prefix. Server-backed (skips offline): the real `PostgresAuditSink` writes a linked chain that
`verify_chain` confirms, and mutating a stored row makes it fail.
"""

import asyncio

from chemclaw.agent.audit import AuditEvent
from chemclaw.agent.audit_store import PostgresAuditSink, chain_hash
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.ids import stable_hash
from chemclaw.durable.audit_chain import ChainCheck, ChainRow, check_chain, verify_chain
from tests.pg import migrated_db_or_skip

_DEFAULT_ACTOR = "u-1"


def _event(tool: str, *, actor: str = _DEFAULT_ACTOR, detail: str = "") -> AuditEvent:
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
    rows = _linked([_event("find_notes"), _event("compute_dft_energy"), _event("expand_note")])
    assert check_chain(rows) == []


def test_check_chain_flags_a_mutated_row() -> None:
    """Altering a stored row's audited field (without re-hashing) is detected as tampering."""
    rows = _linked([_event("find_notes"), _event("compute_dft_energy")])
    tampered = rows[1]._replace(event=_event("compute_dft_energy", actor="attacker"))
    problems = check_chain([rows[0], tampered])
    assert any("tampered" in p for p in problems)


def test_check_chain_flags_a_deleted_row() -> None:
    """Dropping a middle row breaks the prev_hash link of the row that followed it."""
    rows = _linked([_event("a"), _event("b"), _event("c")])
    problems = check_chain([rows[0], rows[2]])  # row 2 removed
    assert any("broken link" in p for p in problems)


def test_check_chain_flags_a_deleted_prefix() -> None:
    """Dropping the leading rows leaves a self-consistent chain the genesis anchor still catches."""
    rows = _linked([_event("a"), _event("b"), _event("c")])
    # Remove rows 1-2: row 3 now leads but its prev_hash points at the deleted row 2 (non-empty).
    problems = check_chain([rows[2]])
    assert any("genesis" in p for p in problems)


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
        # `verify_chain` reads the whole table, so this test needs one to itself. That is what the
        # `chemclaw_test` schema is for (tests/pg.py) — this TRUNCATE would otherwise wipe the real
        # GxP audit trail, and the tamper below would leave it permanently unverifiable.
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE audit_events RESTART IDENTITY")
            await conn.commit()

        sink = PostgresAuditSink()
        await sink.record(_event("find_notes"))
        await sink.record(_event("compute_dft_energy"))
        await sink.record(_event("expand_note"))

        assert await verify_chain() == []  # a freshly written chain verifies

        # Mutate one stored row's audited field without recomputing its hash → chain breaks.
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("UPDATE audit_events SET actor = 'attacker' WHERE id = 2")
            await conn.commit()
        assert await verify_chain() != []

        # Put it back: the tamper is the assertion, not a state the test is entitled to leave
        # behind. A corrupted row outlives the run and fails every later `make audit-verify`.
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("UPDATE audit_events SET actor = %s WHERE id = 2", (_DEFAULT_ACTOR,))
            await conn.commit()
        assert await verify_chain() == []  # and the restore is proven, not assumed

    asyncio.run(_run())


# --- the record grows without invalidating what is already in it -------------------------
# (D-2026-07-31-the-audit-chain-is-versioned)


# The pre-versioning hash, reimplemented from the old source rather than called through
# `chain_hash`.
# That independence is the whole point: a helper that built v1 rows *with* the function under test
# moves with it, so deleting the version switch leaves both sides agreeing and the tests green
# while the mechanism does nothing. Verified rather than assumed — the first draft of this file did
# exactly that, and the two most important tests below passed with the switch removed.
_V1_HASH_FIELDS = (
    "correlation_id",
    "actor",
    "tool",
    "arguments",
    "outcome",
    "detail",
    "latency_ms",
    "revision",
)


def _legacy_chain_hash(prev_hash: str, event: AuditEvent) -> str:
    """`chain_hash` exactly as it was before the event grew — the bytes real v1 rows carry."""
    payload = {field: getattr(event, field) for field in _V1_HASH_FIELDS}
    return stable_hash({"prev": prev_hash, "event": payload}, chars=64)


def test_the_versioned_hash_reproduces_the_legacy_bytes_exactly() -> None:
    """v1 must be the *old* hash, not merely a different one — otherwise history still fails.

    `stable_hash` canonicalizes with `sort_keys=True`, so selecting the eight original keys
    serializes byte-identically to what the narrower model used to dump. This asserts that rather
    than assuming it.
    """
    event = _event("find_notes").model_copy(update={"session_id": "s-1", "purpose": "why"})
    assert chain_hash("abc", event, version=1) == _legacy_chain_hash("abc", event)


def _linked_v1(events: list[AuditEvent], *, start: int = 1) -> list[ChainRow]:
    """Rows as the pre-versioning writer produced them: hashed over the eight original fields."""
    rows: list[ChainRow] = []
    prev = ""
    for i, event in enumerate(events, start=start):
        row_hash = _legacy_chain_hash(prev, event)
        rows.append(ChainRow(id=i, prev_hash=prev, row_hash=row_hash, event=event, chain_version=1))
        prev = row_hash
    return rows


def test_a_v1_row_still_verifies_after_the_event_grew() -> None:
    """The property this versioning exists for.

    `chain_hash` covers the whole `AuditEvent`, so adding `session_id`/`purpose` changes what every
    *historical* row would hash to. Without a per-row version the first deployment to run this
    migration would report its entire trail as tampered with — and a compliance record that accuses
    itself is worse than one that says nothing, because "was it altered, or did we change the
    schema?" is exactly what an auditor needs answered and would no longer be answerable.
    """
    rows = _linked_v1([_event("find_notes"), _event("predict_pka")])
    assert check_chain(rows) == []


def test_a_trail_spanning_the_migration_verifies_end_to_end() -> None:
    """A real deployment's table holds both shapes: rows from before the migration and after it."""
    old = _linked_v1([_event("find_notes"), _event("expand_note")])
    prev = old[-1].row_hash
    new: list[ChainRow] = []
    for i, event in enumerate([_event("predict_pka"), _event("gather_evidence")], start=3):
        # v2 events carry the new fields; the chain continues from the v1 tip.
        event = event.model_copy(update={"session_id": "s-1"})
        row_hash = chain_hash(prev, event)
        new.append(ChainRow(id=i, prev_hash=prev, row_hash=row_hash, event=event))
        prev = row_hash
    assert check_chain([*old, *new]) == []


def test_tampering_with_a_v1_row_is_still_caught() -> None:
    """Versioning must not become a way to launder a modified row: v1 rows stay tamper-evident."""
    rows = _linked_v1([_event("find_notes"), _event("predict_pka")])
    tampered = rows[1]._replace(event=_event("predict_pka", actor="attacker"))
    assert any("tampered" in problem for problem in check_chain([rows[0], tampered]))


def test_the_new_fields_are_covered_by_the_v2_hash() -> None:
    """`session_id` is audited data, not metadata — altering it must break the row's hash."""
    event = _event("find_notes").model_copy(update={"session_id": "s-1"})
    forged = event.model_copy(update={"session_id": "s-2"})
    assert chain_hash("", event) != chain_hash("", forged)


def test_a_v1_row_rehashed_as_v2_does_not_verify() -> None:
    """The versions are genuinely different hashes, so the switch is doing real work.

    If v1 and v2 happened to agree, every test above would pass while the mechanism did nothing.
    """
    event = _event("find_notes")
    assert chain_hash("", event, version=1) != chain_hash("", event)


def test_paging_the_walk_does_not_change_the_verdict() -> None:
    """The fold carries the chain link across pages, so page size cannot alter the answer.

    `verify_chain` used to `fetchall()` the whole table — and `audit_events` is the one table
    `durable/retention.py` refuses to prune, because deleting from a hash chain is
    indistinguishable from the tampering the chain detects. So the one table with no upper bound
    was read whole into the shared background worker (DARK-6). Paging fixes that only if the
    invariant survives a boundary, which is what this pins: an intact chain stays clean at every
    page size, and a tampered row is still caught when the tamper falls exactly on a boundary.
    """
    rows = _linked([_event(f"tool-{i}") for i in range(1, 8)])

    for page_size in range(1, len(rows) + 2):
        check = ChainCheck()
        for start in range(0, len(rows), page_size):
            check.feed(rows[start : start + page_size])
        assert check.problems == [], f"an intact chain reported problems at page size {page_size}"

    # The link between row 3 and row 4 is what a 3-row page boundary sits on; breaking row 4's
    # `prev_hash` must be caught there and not silently start a fresh chain.
    broken = [*rows[:3], rows[3]._replace(prev_hash="deadbeef"), *rows[4:]]
    check = ChainCheck()
    check.feed(broken[:3])
    check.feed(broken[3:])
    assert any("broken link" in problem for problem in check.problems)


def test_a_page_boundary_cannot_manufacture_a_second_genesis() -> None:
    """The sharpest paging failure: each page verifying itself and every page looking intact.

    A fold that reset between pages would treat every page's first row as a genesis, so an
    interior deletion falling on a boundary would vanish — the chain would report clean while
    missing rows, which is precisely the outcome the chain exists to make impossible.
    """
    rows = _linked([_event(f"tool-{i}") for i in range(1, 7)])
    with_gap = [*rows[:2], *rows[3:]]  # row 3 deleted, and the gap lands on the boundary below

    check = ChainCheck()
    check.feed(with_gap[:2])
    check.feed(with_gap[2:])
    assert any("broken link" in problem for problem in check.problems)
