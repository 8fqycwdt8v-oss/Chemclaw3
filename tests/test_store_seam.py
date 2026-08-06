"""Where the store seam's two halves disagreed, and the plumbing that hid it.

Four rows, one theme: a `Protocol` with two implementations is a promise that they answer the same
question the same way, and nothing was checking. The plumbing row is here too because it is the
reason — the same six lines written fourteen times is a place a rule can be omitted once and never
noticed.

- `migrate()` ran against the calculation database only, so a deployment that split
  `session_store_dsn` off had no `session_messages` table and was told migration succeeded.
- `InMemoryStore.find` raised `TypeError` on a timezone-aware `created_at`; Postgres did not.
- One of three writers of computed payloads refused non-finite floats; two let `NaN` reach the wall.
- The bounded-connection helper was hand-rolled fourteen times, four of them claiming "one place,
  DRY".
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.migrate import MigrationError, migration_targets
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    InMemoryStore,
    StoredResult,
)
from tests.pg import migrated_db_or_skip

# --- Every configured database is migrated ----------------------------------------------------


def test_only_one_target_when_the_session_store_shares_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary deployment, and the one every test and `make up` runs."""
    monkeypatch.setattr(settings, "session_store_dsn", "")
    monkeypatch.setattr(settings, "postgres_migration_dsn", "")
    assert migration_targets() == [settings.postgres_dsn]


def test_a_split_session_database_is_also_migrated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding: six session-layer stores follow a DSN nothing migrated.

    `make db-migrate` reported success having applied the schema to the calculation database only,
    and the front door then failed on its first turn with no `session_messages`.
    """
    monkeypatch.setattr(settings, "postgres_migration_dsn", "")
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://u:p@db1:5432/chem")
    monkeypatch.setattr(settings, "session_store_dsn", "postgresql://u:p@db2:5432/sessions")

    assert migration_targets() == [
        "postgresql://u:p@db1:5432/chem",
        "postgresql://u:p@db2:5432/sessions",
    ]


def test_a_different_credential_on_the_same_database_is_not_a_second_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison is host/port/dbname, not the string — and this is why.

    A split-role deployment writes the same database twice with different credentials. Migrating
    it twice would be harmless but wrong to report, and comparing DSN strings would do exactly
    that.
    """
    monkeypatch.setattr(settings, "postgres_migration_dsn", "")
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://app:p1@db1:5432/chem")
    monkeypatch.setattr(settings, "session_store_dsn", "postgresql://other:p2@db1:5432/chem")
    assert migration_targets() == ["postgresql://app:p1@db1:5432/chem"]


def test_a_split_database_with_a_split_credential_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The combination nothing here can serve, refused loudly rather than half-applied.

    There is one migrator DSN and it names the calculation database, so no credential in this
    configuration can own the schema in the session one. Doing nothing is the bug; guessing the
    runtime credential would fail obscurely at the first `CREATE TABLE`.
    """
    monkeypatch.setattr(settings, "postgres_dsn", "postgresql://app:p@db1:5432/chem")
    monkeypatch.setattr(settings, "postgres_migration_dsn", "postgresql://own:p@db1:5432/chem")
    monkeypatch.setattr(settings, "session_store_dsn", "postgresql://app:p@db2:5432/sessions")

    with pytest.raises(MigrationError, match="no credential"):
        migration_targets()


# --- The two backends answer the same question ------------------------------------------------


def _stored(key: str, created_at: datetime | None) -> StoredResult:
    """One cached calculation, with the timestamp under test."""
    return StoredResult(
        key=CalculationKey(
            calc_type="xtb_energy", calc_version="v1", input_hash=key, params_hash="p"
        ),
        result={"energy_hartree": -1.0},
        created_at=created_at,
    )


def test_find_accepts_a_timezone_aware_created_at() -> None:
    """The finding: `datetime.now(UTC)` is what everything in this repo produces, and it raised.

    `InMemoryStore.find` sorted against a naive `datetime.max`, so an aware `created_at` raised
    `TypeError: can't compare offset-naive and offset-aware datetimes` — on a query the Postgres
    backend answered without complaint. Two implementations of one Protocol disagreeing is the bug
    the Protocol exists to prevent.
    """
    store = InMemoryStore()
    asyncio.run(store.put(_stored("a", datetime.now(UTC))))
    assert asyncio.run(store.find(CalculationQuery()))


def test_naive_and_aware_rows_sort_together() -> None:
    """The mixed case, which is the one a real cache has: old naive rows beside new aware ones.

    A naive value means UTC here for the same reason it does in `timestamptz` — the durable backend
    converts on the way in — so the two orderings must agree rather than merely not raise.
    """
    store = InMemoryStore()
    older = datetime(2026, 1, 1, 12, 0)
    newer = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    asyncio.run(store.put(_stored("older", older)))
    asyncio.run(store.put(_stored("newer", newer)))

    found = asyncio.run(store.find(CalculationQuery()))
    assert [s.key.input_hash for s in found] == ["newer", "older"]


def test_a_since_filter_compares_across_the_two_spellings() -> None:
    """`since`/`until` are caller input, so either spelling arrives — and both used to raise."""
    store = InMemoryStore()
    asyncio.run(store.put(_stored("recent", datetime.now(UTC))))

    naive_cutoff = datetime.utcnow() - timedelta(hours=1)  # noqa: DTZ003 - the shape under test
    assert asyncio.run(store.find(CalculationQuery(since=naive_cutoff)))
    aware_cutoff = datetime.now(UTC) - timedelta(hours=1)
    assert asyncio.run(store.find(CalculationQuery(since=aware_cutoff)))
    assert not asyncio.run(store.find(CalculationQuery(since=datetime.now(UTC))))


# --- Non-finite floats are refused where they are written --------------------------------------


async def _round_trip(payload: dict[str, object]) -> object:
    """Write `payload` into a real `jsonb` column and read it back.

    Driven through the database rather than through `Jsonb.dumps`, which is `None` unless a dumper
    was passed — so an assertion on that attribute passes whenever the guard is *present* and says
    nothing about what reaches the column. That version of this test failed under mutation with a
    `TypeError` from calling `None`, which is a pass for the wrong reason.
    """
    await migrated_db_or_skip()
    async with db.bounded() as conn:
        await conn.execute("CREATE TEMP TABLE probe (value jsonb)")
        await conn.execute("INSERT INTO probe (value) VALUES (%s)", (db.jsonb(payload),))
        cursor = await conn.execute("SELECT value FROM probe")
        row = await cursor.fetchone()
        return None if row is None else row[0]


def test_a_non_finite_float_is_refused_before_the_statement_is_sent() -> None:
    """The finding, one layer down from where it was learned.

    `json.dumps` emits bare `NaN` by default; `jsonb` rejects it, but only after the statement
    reaches the *server*, and a caller that logs and continues turns that into silent data loss — a
    BO campaign read back with no observations at all. The calculation cache and the job record
    write the same kind of payload and had the same hole.

    A `ValueError` here, from this process, is the difference: it names the writer in the stack
    instead of arriving as an `InvalidTextRepresentation` about a column.
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="[Nn]ot JSON compliant|Out of range"):
            asyncio.run(_round_trip({"energy_hartree": bad}))


def test_an_ordinary_payload_still_round_trips() -> None:
    """The bound: strictness must not cost a value a calculator legitimately produces."""
    payload = {"energy_hartree": -154.1, "warnings": [], "converged": True}
    assert asyncio.run(_round_trip(payload)) == payload


def test_every_writer_of_a_computed_payload_goes_through_it() -> None:
    """The rule is only worth what the writers actually use, so this reads the source.

    Three modules write numbers a calculator produced into `jsonb`. One had the guard and two did
    not, and there was no reason for the other two to have learned it separately — which is why the
    rule moved to `core/db` rather than being copied twice more.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    writers = [
        "src/chemclaw/science/calc/postgres_store.py",
        "src/chemclaw/durable/job_record_store.py",
        "src/chemclaw/science/bo/campaign_record_store.py",
    ]
    for name in writers:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        raw = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Jsonb"
        ]
        assert not raw, f"{name} writes a computed payload with a bare Jsonb() at line(s) {raw}"
