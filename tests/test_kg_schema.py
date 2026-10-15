"""The notes schema holds every note the system has, and refuses what `Note` refuses.

`infra/sql/128_kg_notes.sql` is the Postgres half of the note format and `kg/rows.py` its mapping.
These tests do not exercise a store (none reads these tables yet); they prove that the columns fit
the notes that exist: the shipped corpus and a note carrying every field, round-tripped through a
real migrated database, with the derived edges equal to the edges of the NetworkX graph.
"""

import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from chemclaw.agent.leaver import erase_actor
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.jsonb import json_column
from chemclaw.core.logging import redact_secrets
from chemclaw.kg.git_writer import GitNoteWriter
from chemclaw.kg.graph import build_graph, load_notes, note_file_fingerprints
from chemclaw.kg.note import Note, ProcessConditions, Relation, read_note
from chemclaw.kg.record import record_note
from chemclaw.kg.render import render_note
from chemclaw.kg.rows import (
    COLUMN_FIELDS,
    content_sha,
    edge_columns,
    note_columns,
    note_from_columns,
)
from chemclaw.kg.store import GitNoteStore
from tests.kg_repo import notes_repo, write_notes
from tests.pg import migrated_db_or_skip

_SHIPPED = Path(__file__).resolve().parents[1] / "knowledge"

_NOTE_COLUMNS = "id, type, frontmatter, body, created_by, valid_from, valid_to, content_sha"


def _every_field() -> Note:
    """A note whose every field is set to something other than its default."""
    return Note(
        id="kitchen-sink",
        type="reaction",
        compound_smiles="CCO",
        tags=["suzuki", "scale-up"],
        created_by="agent",
        actor="chemist-1",
        source="eln:synthetic",
        confidence=0.7,
        conditions=ProcessConditions(
            temperature_c=80.0,
            time_h=4.0,
            yield_percent=82.5,
            purity_percent=98.0,
            outcome="success",
            major_impurity="homocoupled biaryl",
            impurity_area_percent=0.4,
        ),
        calc_refs=["xtb@6.7:abc123:def456"],
        artifact_refs=["xtb@6.7:abc123:def456#geometry"],
        relations=[
            Relation(
                rel="uses",
                to="compound-thf",
                confidence=0.5,
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 6, 30),
            )
        ],
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        body="Coupled in [[compound-thf]], after [[precursor-of:reaction-eln.123]].",
    )


async def _insert(conn: psycopg.AsyncConnection[Any], note: Note) -> None:
    """Write `note` and its edges the way a store would: one row, its edges, its first revision."""
    row = note_columns(note)
    await conn.execute(
        f"INSERT INTO kg_notes ({_NOTE_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            row["id"],
            row["type"],
            json_column(row["frontmatter"]),
            row["body"],
            row["created_by"],
            row["valid_from"],
            row["valid_to"],
            row["content_sha"],
        ),
    )
    for edge in edge_columns(note):
        await conn.execute(
            "INSERT INTO kg_edges (src, rel, dst, valid_from, valid_to, confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                edge["src"],
                edge["rel"],
                edge["dst"],
                edge["valid_from"],
                edge["valid_to"],
                edge["confidence"],
            ),
        )


async def _read_back(conn: psycopg.AsyncConnection[Any]) -> dict[str, Note]:
    """Every `kg_notes` row as the `Note` it holds."""
    cursor = await conn.execute(f"SELECT {_NOTE_COLUMNS} FROM kg_notes")
    names = [column.name for column in cursor.description or ()]
    return {
        row[0]: note_from_columns(dict(zip(names, row, strict=True)))
        for row in await cursor.fetchall()
    }


async def _empty_tables(conn: psycopg.AsyncConnection[Any]) -> None:
    """Start from no notes. History is not cascaded, so it goes first."""
    await conn.execute("DELETE FROM kg_note_revisions")
    await conn.execute("DELETE FROM kg_notes")


def test_the_shipped_corpus_is_not_empty() -> None:
    """A round trip over zero notes proves nothing, so the corpus under test must exist."""
    assert len(load_notes(_SHIPPED)) >= 40


def test_every_shipped_note_survives_the_columns() -> None:
    """The mapping alone, with no database: a note read back from its columns equals the note."""
    for note in load_notes(_SHIPPED):
        assert note_from_columns(note_columns(note)) == note, note.id


def test_the_test_note_sets_every_field_to_a_non_default() -> None:
    """A field `Note` grows must be added to `_every_field`, or the round trip stops covering it."""
    note = _every_field()
    assert note.model_fields_set == set(Note.model_fields)
    for name, field in Note.model_fields.items():
        assert getattr(note, name) != field.get_default(call_default_factory=True), name


def test_only_the_listed_fields_have_a_column_and_the_rest_are_frontmatter() -> None:
    """`frontmatter` is exactly the fields without a column, with nothing dropped or doubled."""
    row = note_columns(_every_field())
    assert COLUMN_FIELDS <= set(Note.model_fields)
    assert set(row["frontmatter"]) == set(Note.model_fields) - COLUMN_FIELDS
    assert not COLUMN_FIELDS & set(row["frontmatter"])


async def test_every_field_of_a_note_round_trips_through_postgres() -> None:
    """Dates, nested models, lists and the edge window come back equal after a real database."""
    await migrated_db_or_skip()
    note = _every_field()
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        assert (await _read_back(conn)) == {note.id: note}
        await conn.rollback()


async def test_the_shipped_corpus_round_trips_through_postgres_with_its_edges() -> None:
    """Every shipped note comes back equal, and `kg_edges` holds the edges the graph has.

    The edges are compared with what `build_graph` derives from the same files, so a relation
    that only one reading sees (a body link, a frontmatter relation, a dangling target) fails here.
    """
    await migrated_db_or_skip()
    notes = load_notes(_SHIPPED)
    graph = build_graph(_SHIPPED)
    expected_edges = {
        (source, relation.rel, target, relation.valid_from, relation.valid_to, relation.confidence)
        for source, target, data in graph.edges(data=True)
        for relation in data["relations"]
    }
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        for note in notes:
            await _insert(conn, note)
        assert (await _read_back(conn)) == {note.id: note for note in notes}
        cursor = await conn.execute(
            "SELECT src, rel, dst, valid_from, valid_to, confidence FROM kg_edges"
        )
        assert {tuple(row) for row in await cursor.fetchall()} == expected_edges
        await conn.rollback()


def _windowed() -> Note:
    """A note whose edges differ in window and confidence, to be read back one by one."""
    return Note(
        id="windowed-source",
        type="reaction",
        body="Plain citation of [[cited]] and a typed [[precursor-of:made]].",
        relations=[
            Relation(
                rel="uses",
                to="closed",
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 3, 31),
                confidence=0.25,
            ),
            Relation(rel="uses", to="from-april", valid_from=date(2026, 4, 1)),
            Relation(rel="uses", to="until-march", valid_to=date(2026, 3, 31), confidence=1.0),
            Relation(rel="uses", to="always"),
            Relation(rel="supersedes", to="older", confidence=0.0),
        ],
    )


async def test_an_edge_keeps_its_window_and_confidence_and_answers_as_of_as_related_does(
    tmp_path: Path,
) -> None:
    """What the model says about each edge is what the row says, and the range reads like `related`.

    `related(as_of=)` is the reference: the graph applies each edge's own window, and `validity @>`
    must select the same targets on every date, including both bounds of every window.
    """
    await migrated_db_or_skip()
    note = _windowed()
    write_notes(tmp_path, note)
    store = GitNoteStore(tmp_path)
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        cursor = await conn.execute(
            "SELECT rel, dst, valid_from, valid_to, confidence FROM kg_edges WHERE src = %s",
            (note.id,),
        )
        rows = {tuple(row) for row in await cursor.fetchall()}
        assert rows == {
            (r.rel, r.to, r.valid_from, r.valid_to, r.confidence) for r in note.outgoing_relations()
        }
        for as_of in (
            date(2025, 12, 31),
            date(2026, 1, 1),
            date(2026, 3, 31),
            date(2026, 4, 1),
            date(2027, 1, 1),
        ):
            cursor = await conn.execute(
                "SELECT dst FROM kg_edges WHERE src = %s AND rel = 'uses' AND validity @> %s::date "
                "ORDER BY dst",
                (note.id, as_of),
            )
            assert [row[0] for row in await cursor.fetchall()] == await store.related(
                note.id, "uses", as_of=as_of
            ), as_of
        await conn.rollback()


@pytest.mark.parametrize(
    ("window", "as_of", "current"),
    [
        ((None, None), date(2026, 5, 1), True),
        ((date(2026, 5, 1), None), date(2026, 4, 30), False),
        ((date(2026, 5, 1), None), date(2026, 5, 1), True),
        ((None, date(2026, 5, 1)), date(2026, 5, 1), True),
        ((None, date(2026, 5, 1)), date(2026, 5, 2), False),
        ((date(2026, 5, 1), date(2026, 5, 1)), date(2026, 5, 1), True),
        ((date(2026, 1, 1), date(2026, 3, 31)), date(2026, 3, 31) + timedelta(days=1), False),
    ],
)
async def test_the_validity_range_is_is_current(
    window: tuple[date | None, date | None], as_of: date, current: bool
) -> None:
    """`validity @> as_of` agrees with `Note.is_current` at both bounds and where one is open."""
    await migrated_db_or_skip()
    note = Note(id="windowed", type="reaction", valid_from=window[0], valid_to=window[1])
    assert note.is_current(as_of) is current
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        cursor = await conn.execute(
            "SELECT validity @> %s::date FROM kg_notes WHERE id = %s", (as_of, note.id)
        )
        row = await cursor.fetchone()
        assert row is not None and row[0] is current
        await conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('../escape', 'reaction', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('a..b', 'reaction', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('.hidden', 'reaction', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('ends-with-dot.', 'reaction', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('a.lock', 'reaction', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('ok', 'type.lock', 'x')",
        "INSERT INTO kg_notes (id, type, content_sha) VALUES ('ok', 'a/b', 'x')",
        "INSERT INTO kg_notes (id, type, created_by, content_sha) VALUES ('ok', 't', 'robot', 'x')",
        "INSERT INTO kg_notes (id, type, revision, content_sha) VALUES ('ok', 't', 0, 'x')",
    ],
)
async def test_the_notes_table_refuses_what_the_model_refuses(statement: str) -> None:
    """The slug rule of `require_note_slug`, the closed `created_by` and the revision floor."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(statement)
        await conn.rollback()


async def test_a_window_that_ends_before_it_starts_is_refused_on_a_note_and_on_an_edge() -> None:
    """`Note` and `Relation` both refuse it; so does the range the table builds from the dates."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, Note(id="source", type="reaction"))
        for statement in (
            "INSERT INTO kg_notes (id, type, valid_from, valid_to, content_sha) "
            "VALUES ('ok', 't', '2026-02-01', '2026-01-01', 'x')",
            "INSERT INTO kg_edges (src, rel, dst, valid_from, valid_to) "
            "VALUES ('source', 'cites', 'target', '2026-02-01', '2026-01-01')",
        ):
            with pytest.raises(psycopg.errors.DataException):
                await conn.execute(statement)
            await conn.rollback()
            await _insert(conn, Note(id="source", type="reaction"))


async def test_an_edge_refuses_a_confidence_outside_zero_to_one() -> None:
    """The bound `Relation.confidence` has, held again where a store would write it."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, Note(id="source", type="reaction"))
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO kg_edges (src, rel, dst, confidence) "
                "VALUES ('source', 'cites', 'target', 1.5)"
            )
        await conn.rollback()


async def test_edges_may_name_an_id_nothing_defines_and_follow_their_note() -> None:
    """A dangling target is a bare node, and deleting the note ends its edges."""
    await migrated_db_or_skip()
    note = _every_field()
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        cursor = await conn.execute("SELECT dst FROM kg_edges WHERE src = %s", (note.id,))
        targets = {row[0] for row in await cursor.fetchall()}
        assert {"compound-thf", "reaction-eln.123"} <= targets
        assert "compound-thf" not in await _read_back(conn)
        await conn.execute("DELETE FROM kg_notes WHERE id = %s", (note.id,))
        cursor = await conn.execute("SELECT count(*) FROM kg_edges WHERE src = %s", (note.id,))
        row = await cursor.fetchone()
        assert row is not None and row[0] == 0
        await conn.rollback()


async def test_a_revision_belongs_to_one_note_and_outlives_a_delete() -> None:
    """Keyed by `(note, revision)`, never orphaned, and a note's history blocks its deletion."""
    await migrated_db_or_skip()
    note = _every_field()
    row = note_columns(note)
    revision = (
        "INSERT INTO kg_note_revisions (note_id, revision, type, frontmatter, body, created_by, "
        "content_sha) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )

    def values(note_id: str, number: int) -> tuple[Any, ...]:
        return (
            note_id,
            number,
            row["type"],
            json_column(row["frontmatter"]),
            row["body"],
            row["created_by"],
            row["content_sha"],
        )

    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        await conn.execute(revision, values(note.id, 1))
        await conn.execute(revision, values(note.id, 2))
        for error, statement, params in (
            (psycopg.errors.UniqueViolation, revision, values(note.id, 2)),
            (psycopg.errors.ForeignKeyViolation, revision, values("no-such-note", 1)),
            (psycopg.errors.ForeignKeyViolation, "DELETE FROM kg_notes WHERE id = %s", (note.id,)),
        ):
            with pytest.raises(error):
                await conn.execute(statement, params)
            await conn.rollback()
            await _empty_tables(conn)
            await _insert(conn, note)
            await conn.execute(revision, values(note.id, 1))
            await conn.execute(revision, values(note.id, 2))
        await conn.rollback()


async def test_the_index_row_gets_a_key_to_its_note_without_the_previous_image_noticing() -> None:
    """The expand step: an unkeyed row lands, a wrong or half key is refused, a right one follows.

    The row goes with the note it indexes, and the key is indexed so that delete is not a scan.
    """
    await migrated_db_or_skip()
    note = Note(id="indexed", type="reaction")
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        await conn.execute("DELETE FROM note_index WHERE note_id LIKE 'kgschema-%'")
        await conn.execute("INSERT INTO note_index (note_id) VALUES ('kgschema-unkeyed')")
        for columns, values in (
            ("kg_tenant_id, kg_note_id", ("default", "no-such")),
            ("kg_tenant_id, kg_note_id", ("other-tenant", note.id)),
            ("kg_note_id", (note.id,)),
        ):
            placeholders = ", ".join(["%s"] * len(values))
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                await conn.execute(
                    f"INSERT INTO note_index (note_id, {columns}) "
                    f"VALUES ('kgschema-bad', {placeholders})",
                    values,
                )
            await conn.rollback()
            await _empty_tables(conn)
            await _insert(conn, note)
        await conn.execute(
            "INSERT INTO note_index (note_id, kg_tenant_id, kg_note_id) "
            "VALUES ('kgschema-b', 'default', %s)",
            (note.id,),
        )
        await conn.execute("DELETE FROM kg_notes WHERE id = %s", (note.id,))
        cursor = await conn.execute("SELECT count(*) FROM note_index WHERE note_id = 'kgschema-b'")
        row = await cursor.fetchone()
        assert row is not None and row[0] == 0
        cursor = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'note_index' AND indexdef LIKE '%(kg_tenant_id, kg_note_id)%'"
        )
        assert await cursor.fetchone() is not None, "the key to the notes has no index"
        await conn.rollback()


async def test_a_note_id_is_unique_within_a_tenant_and_every_reference_stays_within_it() -> None:
    """Keys lead with `tenant_id`: two tenants may share an id and cannot reach each other."""
    await migrated_db_or_skip()
    note = Note(id="shared-id", type="reaction")
    insert = "INSERT INTO kg_notes (tenant_id, id, type, content_sha) VALUES (%s, %s, 't', 'x')"
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        for tenant in ("t1", "t2", "default"):
            await conn.execute(insert, (tenant, note.id))
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(insert, ("t1", note.id))
        await conn.rollback()
        await _empty_tables(conn)
        await conn.execute(insert, ("t1", "only-in-t1"))
        for statement in (
            "INSERT INTO kg_edges (tenant_id, src, rel, dst) "
            "VALUES ('t2', 'only-in-t1', 'cites', 'x')",
            "INSERT INTO kg_note_revisions (tenant_id, note_id, revision, type, frontmatter, body, "
            "created_by, content_sha) VALUES ('t2', 'only-in-t1', 1, 't', '{}', '', 'agent', 'x')",
        ):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                await conn.execute(statement)
            await conn.rollback()
            await conn.execute(insert, ("t1", "only-in-t1"))
        await conn.rollback()


async def test_a_revision_refuses_what_the_note_it_describes_refuses() -> None:
    """An unknown `created_by` or a revision below 1 is refused on the history table too."""
    await migrated_db_or_skip()
    note = Note(id="has-history", type="reaction")
    insert = (
        "INSERT INTO kg_note_revisions (note_id, revision, type, frontmatter, body, created_by, "
        "content_sha) VALUES ('has-history', %s, 'reaction', '{}', '', %s, 'x')"
    )
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        for revision, created_by in ((1, "robot"), (0, "agent")):
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(insert, (revision, created_by))
            await conn.rollback()
            await _insert(conn, note)
        await conn.rollback()


@pytest.mark.parametrize("value", ["a\x00b", "\x00"])
async def test_a_nul_is_refused_by_the_model_and_by_the_database(value: str) -> None:
    """A file holds U+0000 and Postgres does not, so the model refuses what no store can keep."""
    with pytest.raises(ValidationError, match="NUL"):
        Note(id="n", type="reaction", body=value)
    with pytest.raises(ValidationError, match="NUL"):
        Note(id="n", type="reaction", tags=[value])
    with pytest.raises(ValidationError, match="NUL"):
        Note(id="n", type="reaction", relations=[Relation(rel="uses", to=value)])
    with pytest.raises(ValidationError, match="NUL"):
        Note(id="n", type="reaction", conditions=ProcessConditions(major_impurity=value))
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        with pytest.raises(psycopg.DataError):
            await conn.execute(
                "INSERT INTO kg_notes (id, type, body, content_sha) VALUES ('n', 't', %s, 'x')",
                (value,),
            )
        await conn.rollback()
        with pytest.raises(psycopg.DataError):
            await conn.execute(
                "INSERT INTO kg_notes (id, type, frontmatter, content_sha) "
                "VALUES ('n', 't', %s, 'x')",
                (json_column({"source": value}),),
            )
        await conn.rollback()


@pytest.mark.parametrize("value", ["ends-with-dot.", "a.lock", ".hidden", "a..b", "a/b", "", "-x"])
def test_the_sql_slug_rule_is_the_models(value: str) -> None:
    """The ids the table's CHECKs refuse are ids `Note` refuses, so a row never needs tolerance."""
    with pytest.raises(ValidationError):
        Note(id=value, type="reaction")
    with pytest.raises(ValidationError):
        Note(id="ok", type=value)


def test_content_sha_survives_a_round_trip_through_the_canonical_file(tmp_path: Path) -> None:
    """Rendering a note, redacted, and reading it back gives a note with the same digest.

    This is what makes the digest a property of the note and not of how it was laid out: a hand
    formatted file and its canonical render are one note with one `content_sha`.
    """
    for note in load_notes(_SHIPPED):
        path = tmp_path / f"{note.id}.md"
        path.write_text(redact_secrets(render_note(note)), encoding="utf-8")
        again = read_note(path)
        assert again is not None and content_sha(again) == content_sha(note), note.id


def test_a_shipped_file_has_the_digest_of_its_note_exactly_when_it_is_the_canonical_render() -> (
    None
):
    """`content_sha` is not the file fingerprint, and the difference is the file's layout.

    The shipped notes were written by hand as well as by the writer, so some files differ from
    their canonical render (29 of 41 are identical at the time of writing). For each note the two
    digests agree when, and only when, the file's bytes are the canonical text, which is why the
    flip re-embeds a note once and `--compare` compares notes and edges rather than fingerprints.
    """
    fingerprints = note_file_fingerprints(_SHIPPED)
    notes = {note.id: note for note in load_notes(_SHIPPED)}
    assert set(notes) <= set(fingerprints)
    for path in _SHIPPED.rglob("*.md"):
        note = notes.get(path.stem)
        if note is None:
            continue
        canonical = path.read_text(encoding="utf-8") == redact_secrets(render_note(note))
        assert (fingerprints[note.id] == content_sha(note)) is canonical, note.id


async def test_a_note_the_writer_recorded_has_the_digest_of_the_file_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real writer, credentials redacted: the file's fingerprint is `content_sha`.

    The digest of the unredacted render differs from both, which is the defect a digest over the
    model would have: the stored text and the hash would describe different notes.
    """
    clone = notes_repo(tmp_path)
    monkeypatch.setattr(settings, "note_repo_dir", str(clone))
    note = Note(
        id="writer-made",
        type="playbook",
        created_by="agent",
        body="Connect with postgresql://admin:hunter2secret@db.internal/chem to read it.",
    )
    writer = GitNoteWriter(repo_dir=str(clone), base_branch="main", remote="origin")
    await record_note(note, writer)
    written = (clone / "knowledge" / "playbook" / "writer-made.md").read_text(encoding="utf-8")
    assert "hunter2secret" not in written
    fingerprints = await GitNoteStore(clone / "knowledge").fingerprints()
    fingerprint = fingerprints["writer-made"]
    assert fingerprint == content_sha(note)
    unredacted = hashlib.sha256(render_note(note).encode("utf-8")).hexdigest()
    assert fingerprint != f"sha256:{unredacted}"


async def test_an_erasure_counts_the_notes_and_revisions_written_for_a_person_and_keeps_them() -> (
    None
):
    """The record is retained: a note names the person it was written for, a revision its author.

    `created_by` is `agent` here, which names nobody; the person is `actor` in the frontmatter and
    `recorded_by` on the revision.
    """
    await migrated_db_or_skip()
    note = _every_field()
    row = note_columns(note)
    async with await connect(settings.postgres_dsn) as conn:
        await _empty_tables(conn)
        await _insert(conn, note)
        await conn.execute(
            "INSERT INTO kg_note_revisions (note_id, revision, type, frontmatter, body, "
            "created_by, content_sha, recorded_by) VALUES (%s, 1, %s, %s, %s, %s, %s, %s)",
            (
                note.id,
                row["type"],
                json_column({}),
                row["body"],
                row["created_by"],
                row["content_sha"],
                "editor-2",
            ),
        )
        await conn.commit()
    try:
        written_for = await erase_actor("chemist-1")
        edited_by = await erase_actor("editor-2")
        assert written_for.retained["kg_notes"] == 1
        assert written_for.retained["kg_note_revisions"] == 0
        assert edited_by.retained["kg_notes"] == 0
        assert edited_by.retained["kg_note_revisions"] == 1
    finally:
        async with await connect(settings.postgres_dsn) as conn:
            await _empty_tables(conn)
            await conn.commit()
