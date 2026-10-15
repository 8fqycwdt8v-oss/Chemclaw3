"""What the restore drill writes into a database and asks of it afterwards.

Database administration, so it lives beside `migrate.py` and `grants.py`: `cli/restore_drill.py`
owns the containers and the clock and calls this module with a DSN, never a connection, which
keeps the entrypoint layer free of the driver. A *snapshot* is everything compared between the
primary at the target instant and the restored cluster: row counts, the migration ledger, every
index, the runtime role's privileges and the `vector` extension; `compare_snapshots` is pure, so
a mismatch is a list of named checks.

The fixture is plain SQL on purpose: a store's own writer reads the process-wide DSN, and the drill
must work against a server no `Settings` points at. Every row carries a tag, so three waves (before
the base backup, between backup and target, after the target) never collide, and the last is exactly
what a correct restore must not contain.
"""

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg import sql

Status = Literal["pass", "fail", "skip"]

#: The label the marker rows carry; the restore must hold the first and never the second.
MARKER_AT_TARGET = "target"
MARKER_AFTER_TARGET = "after-target"

#: The role the runtime principal splits into (`infra/sql/grants/app_privileges.sql`).
APP_ROLE = "chemclaw_app"

#: Tables the wave after the target changes, so a restore that ignored the target is visible.
POST_TARGET_TABLES = ("session_messages", "audit_events", "calculation_results", "session_owners")

#: Width of `molecule_fingerprints.bits` and of `note_index.embedding` (migrations 002 and 012).
_FINGERPRINT_BITS = 2048
_EMBEDDING_DIMENSIONS = 1536

_AUDIT_INSERT = (
    "INSERT INTO audit_events (correlation_id, actor, tool, arguments, outcome, latency_ms) "
)

#: A table `COUNT(*)` may not run against while it is being created or dropped; none are.
_COUNT_SQL = "SELECT count(*) FROM {table}"


@dataclass(frozen=True)
class Check:
    """One named verification: it passed, failed with a reason, or was skipped with a reason."""

    name: str
    status: Status
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        """The JSON form the drill's report carries."""
        return {"name": self.name, "status": self.status, "detail": self.detail}


def passed(name: str, detail: str = "") -> Check:
    """A check that held."""
    return Check(name, "pass", detail)


def failed(name: str, detail: str) -> Check:
    """A check that did not hold; `detail` says what differed."""
    return Check(name, "fail", detail)


def skipped(name: str, reason: str) -> Check:
    """A check that could not run here; the reason is part of the report, never implied."""
    return Check(name, "skip", reason)


@dataclass(frozen=True)
class Snapshot:
    """What the drill compares between the primary at the target and the restored cluster."""

    counts: Mapping[str, int]
    ledger: Sequence[tuple[str, str]]
    indexes: Sequence[tuple[str, str, str]]
    invalid_indexes: Sequence[str]
    grants: Sequence[tuple[str, str, str]]
    vector_version: str | None
    markers: Sequence[str] = field(default_factory=tuple)


def _count_rows(conn: psycopg.Connection[Any], table: str) -> int:
    """The exact row count of `table`."""
    row = conn.execute(_COUNT_SQL.format(table=f'"{table}"')).fetchone()
    return int(row[0]) if row else 0


def _snapshot(conn: psycopg.Connection[Any]) -> Snapshot:
    """Read a snapshot. Counts are exact (`count(*)`), not planner estimates.

    Run only while nothing else writes to the database: the drill's own connection is the sole
    writer to the application database, which is what makes the counts comparable.
    """
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    ]
    counts = {table: _count_rows(conn, table) for table in tables}
    ledger = [
        (str(r[0]), str(r[1]))
        for r in conn.execute("SELECT filename, checksum FROM schema_migrations ORDER BY filename")
    ]
    indexes = [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "ORDER BY tablename, indexname"
        )
    ]
    invalid = [
        str(r[0])
        for r in conn.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE NOT i.indisvalid ORDER BY 1"
        )
    ]
    grants = [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT table_name, privilege_type, grantee FROM information_schema.role_table_grants "
            "WHERE grantee = %s AND table_schema = 'public' ORDER BY 1, 2",
            (APP_ROLE,),
        )
    ]
    version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = version.fetchone()
    markers = (
        [str(r[0]) for r in conn.execute("SELECT label FROM restore_drill_marker ORDER BY id")]
        if "restore_drill_marker" in counts
        else []
    )
    return Snapshot(counts, ledger, indexes, invalid, grants, str(row[0]) if row else None, markers)


def diff_counts(expected: Mapping[str, int], restored: Mapping[str, int]) -> list[str]:
    """One line per table whose count differs, or that exists on only one side."""
    lines = [
        f"{table}: expected {expected[table]}, restored {restored[table]}"
        for table in sorted(expected.keys() & restored.keys())
        if expected[table] != restored[table]
    ]
    lines += [
        f"{table}: missing from the restored database"
        for table in sorted(expected.keys() - restored.keys())
    ]
    lines += [
        f"{table}: not in the primary at the target"
        for table in sorted(restored.keys() - expected.keys())
    ]
    return lines


def compare_snapshots(expected: Snapshot, restored: Snapshot) -> list[Check]:
    """The structural checks between the primary at the target instant and the restored cluster."""
    checks: list[Check] = []
    drift = diff_counts(expected.counts, restored.counts)
    checks.append(
        failed("row_counts", "; ".join(drift))
        if drift
        else passed(
            "row_counts", f"{len(expected.counts)} tables, {sum(expected.counts.values())} rows"
        )
    )
    checks.append(
        passed("schema_migrations", f"{len(expected.ledger)} files, checksums identical")
        if expected.ledger and list(expected.ledger) == list(restored.ledger)
        else failed(
            "schema_migrations",
            f"ledger differs or is empty: {len(expected.ledger)} files expected, "
            f"{len(restored.ledger)} restored",
        )
    )
    missing = sorted(set(expected.indexes) - set(restored.indexes))
    extra = sorted(set(restored.indexes) - set(expected.indexes))
    if missing or extra or restored.invalid_indexes:
        checks.append(
            failed(
                "indexes",
                f"missing {[m[1] for m in missing]}, unexpected {[e[1] for e in extra]}, "
                f"invalid {list(restored.invalid_indexes)}",
            )
        )
    else:
        vector_indexes = [i for i in restored.indexes if "USING hnsw" in i[2]]
        checks.append(
            passed("indexes", f"{len(restored.indexes)} indexes valid, {len(vector_indexes)} hnsw")
            if vector_indexes
            else failed("indexes", "no hnsw index in the restored database")
        )
    checks.append(
        passed("pgvector", f"extension {restored.vector_version}")
        if restored.vector_version and restored.vector_version == expected.vector_version
        else failed(
            "pgvector", f"expected {expected.vector_version}, restored {restored.vector_version}"
        )
    )
    checks.append(
        passed("grants", f"{len(restored.grants)} privileges for {APP_ROLE}")
        if restored.grants and list(restored.grants) == list(expected.grants)
        else failed("grants", f"{len(expected.grants)} expected, {len(restored.grants)} restored")
    )
    return checks


def marker_check(restored_markers: Sequence[str]) -> Check:
    """The restored database holds the target marker and none written after it.

    This is the one check that proves recovery stopped at the instant asked for: counts alone
    cannot tell a restore that stopped early from one that stopped exactly there.
    """
    if MARKER_AT_TARGET not in restored_markers:
        return failed(
            "marker", f"the '{MARKER_AT_TARGET}' marker is missing: recovery stopped early"
        )
    if MARKER_AFTER_TARGET in restored_markers:
        return failed("marker", f"'{MARKER_AFTER_TARGET}' is present: recovery ran past the target")
    return passed("marker", f"recovered to the '{MARKER_AT_TARGET}' marker and no further")


def post_target_check(restored: Mapping[str, int], primary_final: Mapping[str, int]) -> Check:
    """The wave written after the target is absent from the restore, table by table."""
    untouched = [t for t in POST_TARGET_TABLES if restored.get(t) == primary_final.get(t)]
    return (
        failed("post_target_rows_excluded", f"identical on both sides: {untouched}")
        if untouched
        else passed(
            "post_target_rows_excluded", f"{list(POST_TARGET_TABLES)} differ from the primary"
        )
    )


def _bits(rng: random.Random) -> str:
    """A reproducible 2048-bit string, as `molecule_fingerprints.bits` stores."""
    return "".join(rng.choice("01") for _ in range(_FINGERPRINT_BITS))


def _vector(rng: random.Random) -> str:
    """A reproducible pgvector literal of the width `note_index.embedding` declares."""
    return "[" + ",".join(f"{rng.random():.4f}" for _ in range(_EMBEDDING_DIMENSIONS)) + "]"


def calculation_key(tag: str) -> str:
    """A calculation key for `tag` in the shape a note may cite (`type@version:input:params`)."""
    return f"drill@v1:in-{tag}:pa-{tag}"


def _create_marker_table(conn: psycopg.Connection[Any]) -> None:
    """The table the drill's marker rows live in; created in the primary, restored with it."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS restore_drill_marker ("
        "id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL, "
        "committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
    )
    conn.commit()


def _write_marker(conn: psycopg.Connection[Any], label: str) -> None:
    """Commit one marker row."""
    conn.execute("INSERT INTO restore_drill_marker (label) VALUES (%s)", (label,))
    conn.commit()


def _load_rows(conn: psycopg.Connection[Any], tag: str, rows: int, seed: int) -> None:
    """Write `rows` of every kind of state the system keeps, all carrying `tag`.

    Conversation (`session_*`), the audit trail, the calculation cache, the publish outbox, the
    fingerprint and note indexes (vector columns with HNSW indexes), the LangGraph checkpointer and,
    when the schema has them, the knowledge graph. Only columns without a default are named, so a
    migration that adds a defaulted column does not break the drill.
    """
    rng = random.Random(seed)
    for i in range(rows):
        name = f"{tag}-{i}"
        conn.execute(
            "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s)", (name, "drill")
        )
        for turn in range(2):
            conn.execute(
                "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                (name, json.dumps({"type": "human" if turn == 0 else "ai", "content": name})),
            )
        conn.execute(
            _AUDIT_INSERT + "VALUES (%s, 'drill', 'restore_drill', %s, 'ok', 1.5)",
            (name, json.dumps({"row": name})),
        )
        conn.execute(
            "INSERT INTO calculation_results (key, calc_type, calc_version, input_hash, "
            "params_hash, result) VALUES (%s, 'drill', 'v1', %s, %s, %s)",
            (
                calculation_key(name),
                f"in-{name}",
                f"pa-{name}",
                json.dumps({"energy": -rng.random()}),
            ),
        )
        conn.execute(
            "INSERT INTO result_publications (sink, calc_ref, document) VALUES ('drill', %s, %s)",
            (name, json.dumps({"ref": name})),
        )
        conn.execute(
            "INSERT INTO molecule_fingerprints (id, label, bits, definition) "
            "VALUES (%s, %s, %s::bit(2048), 'ecfp:r2:b2048')",
            (name, name, _bits(rng)),
        )
        conn.execute(
            "INSERT INTO note_index (note_id, embedding) VALUES (%s, %s::vector)",
            (name, _vector(rng)),
        )
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, "
            "metadata) VALUES (%s, '', %s, 'json', %s, '{}')",
            (name, f"cp-{name}", json.dumps({"v": 1, "id": f"cp-{name}"})),
        )
        conn.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', 'messages', '1', 'json', %s)",
            (name, b"{}"),
        )
        conn.execute(
            "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
            "channel, type, blob) VALUES (%s, '', %s, 't', 0, 'messages', 'json', %s)",
            (name, f"cp-{name}", b"{}"),
        )
    load_knowledge_rows(conn, tag, rows)
    conn.commit()


def _knowledge_tables(conn: psycopg.Connection[Any]) -> list[str]:
    """The `kg_*` tables this schema has; empty until the knowledge graph moves into Postgres."""
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'kg\\_%' ORDER BY 1"
        )
    ]


def load_knowledge_rows(conn: psycopg.Connection[Any], tag: str, rows: int) -> None:
    """Notes, their revisions and edges, when `kg_notes` exists; a no-op before then.

    Written against `kg_notes`, `kg_note_revisions` and `kg_edges` by their required columns. A
    schema that renames one fails the drill here, by name, rather than skipping the tables a restore
    must prove.
    """
    if "kg_notes" not in _knowledge_tables(conn):
        return
    for i in range(rows):
        note = f"drill-note-{tag}-{i}"
        conn.execute(
            "INSERT INTO kg_notes (id, type, content_sha, created_by) "
            "VALUES (%s, 'finding', %s, 'agent')",
            (note, f"sha-{note}"),
        )
        conn.execute(
            "INSERT INTO kg_note_revisions (note_id, revision, type, frontmatter, body, "
            "created_by, content_sha) VALUES (%s, 1, 'finding', '{}', %s, 'agent', %s)",
            (note, f"body of {note}", f"sha-{note}"),
        )
        if i > 0:
            conn.execute(
                "INSERT INTO kg_edges (src, rel, dst) VALUES (%s, 'cites', %s)",
                (note, f"drill-note-{tag}-{i - 1}"),
            )


def _change_rows(conn: psycopg.Connection[Any], tag: str) -> None:
    """An update and a delete, so replay is shown to carry more than inserts."""
    conn.execute(
        "UPDATE result_publications SET state = 'delivered', delivered_at = now() "
        "WHERE calc_ref = %s",
        (f"{tag}-0",),
    )
    conn.execute("DELETE FROM session_messages WHERE session_id = %s", (f"{tag}-1",))
    conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (f"{tag}-1",))
    conn.commit()


def _disaster(conn: psycopg.Connection[Any]) -> None:
    """What the restore exists for: the conversation table emptied after the target instant."""
    conn.execute("DELETE FROM session_messages")
    conn.commit()


def _create_app_role(conn: psycopg.Connection[Any], password: str) -> None:
    """The split runtime principal, before the grants that name it (`make db-grants`)."""
    # `CREATE ROLE` takes no bind parameters, so the password is rendered as a literal.
    conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(APP_ROLE), sql.Literal(password)
        )
    )
    conn.commit()


def _nearest_fingerprint(conn: psycopg.Connection[Any], probe_id: str) -> tuple[str, str]:
    """The nearest molecule to `probe_id` by Jaccard distance, and the plan that found it.

    Sequential scans are disabled so the plan names the HNSW index if it is usable: an index that
    was restored but is invalid, or absent, falls back to a scan and the check says so.
    """
    conn.execute("SET enable_seqscan = off")
    try:
        sql = (
            "SELECT id FROM molecule_fingerprints WHERE id <> %s "
            "ORDER BY bits <%%> (SELECT bits FROM molecule_fingerprints WHERE id = %s LIMIT 1) "
            "LIMIT 1"
        )
        plan = "\n".join(str(r[0]) for r in conn.execute("EXPLAIN " + sql, (probe_id, probe_id)))
        row = conn.execute(sql, (probe_id, probe_id)).fetchone()
    finally:
        conn.execute("RESET enable_seqscan")
    return (str(row[0]) if row else ""), plan


def _pgvector_check(conn: psycopg.Connection[Any], probe_id: str, expected_id: str) -> Check:
    """The HNSW index answers a nearest-neighbour query with the primary's answer."""
    found, plan = _nearest_fingerprint(conn, probe_id)
    if "molecule_fingerprints_jaccard_idx" not in plan:
        return failed("pgvector_query", f"the planner did not use the HNSW index: {plan!r}")
    if found != expected_id:
        return failed(
            "pgvector_query", f"nearest to {probe_id}: expected {expected_id}, got {found}"
        )
    return passed("pgvector_query", f"HNSW index used; nearest to {probe_id} is {found}")


def notify_check(dsn: str) -> Check:
    """`LISTEN` then `NOTIFY` from another session reaches the listener."""
    channel = "restore_drill"
    with (
        psycopg.connect(dsn, autocommit=True) as listener,
        psycopg.connect(dsn, autocommit=True) as sender,
    ):
        listener.execute(f"LISTEN {channel}")
        sender.execute("SELECT pg_notify(%s, 'ping')", (channel,))
        received = [n.payload for n in listener.notifies(timeout=5.0, stop_after=1)]
    if received != ["ping"]:
        return failed("listen_notify", f"received {received} within 5 s")
    return passed("listen_notify", "a notification from another session was delivered")


def advisory_lock_check(dsn: str) -> Check:
    """A session-level advisory lock excludes a second session and frees on release."""
    key = 0x5245_5354_4F52_45  # "RESTORE"
    with (
        psycopg.connect(dsn, autocommit=True) as first,
        psycopg.connect(dsn, autocommit=True) as second,
    ):
        got = first.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
        contended = second.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
        first.execute("SELECT pg_advisory_unlock(%s)", (key,))
        reacquired = second.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
        second.execute("SELECT pg_advisory_unlock(%s)", (key,))
    ok = got == (True,) and contended == (False,) and reacquired == (True,)
    return (
        passed("advisory_lock", "excluded while held, free after release")
        if ok
        else failed(
            "advisory_lock", f"acquire {got}, contended {contended}, reacquire {reacquired}"
        )
    )


def _writable_check(conn: psycopg.Connection[Any]) -> Check:
    """The promoted cluster accepts writes and its sequences continue past the restored rows."""
    in_recovery = conn.execute("SELECT pg_is_in_recovery()").fetchone()
    if in_recovery != (False,):
        return failed("promoted", "the restored cluster is still in recovery")
    before = conn.execute("SELECT coalesce(max(id), 0) FROM session_messages").fetchone()
    row = conn.execute(
        "INSERT INTO session_messages (session_id, message) "
        "VALUES ('restored-write', '{}') RETURNING id"
    ).fetchone()
    conn.commit()
    if row is None or before is None or int(row[0]) <= int(before[0]):
        return failed("promoted", f"a new id {row} did not follow the restored maximum {before}")
    return passed("promoted", f"accepts writes; next id {row[0]} follows {before[0]}")


def app_role_check(dsn: str) -> Check:
    """As the runtime role: it can append to the audit trail and cannot rewrite it."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_AUDIT_INSERT + "VALUES ('restored', 'drill', 'restore_drill', '{}', 'ok', 0)")
        try:
            conn.execute("UPDATE audit_events SET outcome = 'rewritten'")
        except psycopg.errors.InsufficientPrivilege:
            return passed("app_role", f"{APP_ROLE} appends to audit_events and cannot update it")
    return failed(
        "app_role", f"{APP_ROLE} could rewrite audit_events: the append-only grant is gone"
    )


# --- The connection-free surface: everything `cli/restore_drill.py` calls takes a DSN. ----------


def is_ready(dsn: str, *, recovered: bool = False) -> bool:
    """Whether the server answers (and, if `recovered`, has left recovery); False while not."""
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            row = conn.execute("SELECT pg_is_in_recovery()").fetchone()
    except psycopg.Error:
        return False
    return row == (False,) if recovered else True


def clock_now(dsn: str) -> datetime:
    """The server's `clock_timestamp()`: the instant a restore is asked to stop at."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None:
        raise RuntimeError("the server returned no clock reading")
    instant: datetime = row[0]
    return instant


def switch_wal(dsn: str) -> str:
    """Close the current WAL segment and return its file name, so the caller can wait for it."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT pg_walfile_name(pg_current_wal_lsn())").fetchone()
        conn.execute("SELECT pg_switch_wal()")
    if row is None:
        raise RuntimeError("the server reports no current WAL file")
    return str(row[0])


def archiver_stats(dsn: str) -> tuple[int, int]:
    """`(archived_count, failed_count)` from `pg_stat_archiver`."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT archived_count, failed_count FROM pg_stat_archiver").fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def prepare(dsn: str, app_password: str) -> None:
    """The runtime role the grants name, and the table the marker rows live in."""
    with psycopg.connect(dsn) as conn:
        _create_app_role(conn, app_password)
        _create_marker_table(conn)


def load_wave(dsn: str, tag: str, rows: int, seed: int) -> list[str]:
    """Write one wave of fixture rows; the `kg_*` tables the schema has (empty = none)."""
    with psycopg.connect(dsn) as conn:
        _load_rows(conn, tag, rows, seed)
        return _knowledge_tables(conn)


def change_wave(dsn: str, tag: str) -> None:
    """An update and a delete against the rows of wave `tag`."""
    with psycopg.connect(dsn) as conn:
        _change_rows(conn, tag)


def empty_conversations(dsn: str) -> None:
    """The accident the restore exists for: the conversation table emptied."""
    with psycopg.connect(dsn) as conn:
        _disaster(conn)


def mark(dsn: str, label: str) -> None:
    """Commit one marker row."""
    with psycopg.connect(dsn) as conn:
        _write_marker(conn, label)


def snapshot(dsn: str) -> Snapshot:
    """Read a snapshot of the database at `dsn`."""
    with psycopg.connect(dsn) as conn:
        return _snapshot(conn)


def knowledge_counts(dsn: str) -> dict[str, int]:
    """Row counts of the `kg_*` tables; empty when the schema has none."""
    with psycopg.connect(dsn) as conn:
        return {table: _count_rows(conn, table) for table in _knowledge_tables(conn)}


def nearest_neighbour(dsn: str, probe_id: str) -> str:
    """The id nearest to `probe_id` by Jaccard distance."""
    with psycopg.connect(dsn) as conn:
        return _nearest_fingerprint(conn, probe_id)[0]


def vector_query_check(dsn: str, probe_id: str, expected_id: str) -> Check:
    """The HNSW index answers a nearest-neighbour query with the primary's answer."""
    with psycopg.connect(dsn) as conn:
        return _pgvector_check(conn, probe_id, expected_id)


def promotion_check(dsn: str) -> Check:
    """The promoted cluster accepts writes and its sequences continue past the restored rows."""
    with psycopg.connect(dsn) as conn:
        return _writable_check(conn)
