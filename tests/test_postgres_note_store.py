"""The Postgres note store answers as the git store does, and writes a note in one transaction.

Three kinds of test. The first two hold `PostgresNoteStore` to `GitNoteStore` on the same notes and
need both stores: the committed corpus under `knowledge/`, and synthetic graphs with cycles, retired
notes, dangling targets and windows. The shared suite in `tests/test_note_store.py` asserts fixed
answers on a small graph; these assert that the two stores agree on large ones, question by
question. The rest is what only the database store has: revisions, the transaction, optimistic
concurrency, writers that overlap, tenants, and the indexes the queries rely on. The git store has
no transaction (its writer commits files one after another and rolls back by hand), so the
atomicity tests have no git arm.
"""

import asyncio
import hashlib
import logging
import random
from collections.abc import Awaitable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import redact_secrets
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.postgres_store import PostgresNoteStore
from chemclaw.kg.record import _build_write
from chemclaw.kg.render import render_note
from chemclaw.kg.rows import content_sha
from chemclaw.kg.search import query_terms
from chemclaw.kg.store import (
    GitNoteStore,
    HumanNoteProtected,
    NoteRevisionConflict,
    NoteStore,
    NoteUnit,
)
from tests.note_store_harness import STORES, Harness, empty_note_tables
from tests.pg import planned_index

_SHIPPED = Path(__file__).resolve().parents[1] / "knowledge"


@pytest.fixture
async def store() -> PostgresNoteStore:
    """A store over empty notes tables in the test schema."""
    await empty_note_tables()
    return PostgresNoteStore()


async def _rows(sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
    async with await connect(settings.postgres_dsn) as conn:
        cursor = await conn.execute(sql, tuple(params))
        return await cursor.fetchall()


def _note(note_id: str, body: str = "A note.", **fields: Any) -> Note:
    return Note(id=note_id, type="reaction", created_by="agent", body=body, **fields)


# --- The two stores agree -----------------------------------------------------------------------


async def _outcome(call: Awaitable[Any]) -> Any:
    """The answer, or `KeyError` for the question a store refuses, so refusals compare too."""
    try:
        return await call
    except KeyError:
        return KeyError


def _by_link(edges: list[Any]) -> dict[tuple[str, str], list[Any]]:
    """Edges keyed by link, relations sorted: the order a store lists them in is its own."""
    return {
        (edge.src, edge.dst): sorted(edge.relations, key=lambda r: (r.rel, str(r.valid_from)))
        for edge in edges
    }


def _boundaries(notes: list[Note]) -> list[date]:
    """Days on and around every window edge, where an off-by-one in a range would show."""
    days = {date(2020, 1, 1), date(2026, 10, 15), date(2035, 1, 1)}
    for note in notes:
        for edge in (note.valid_from, note.valid_to):
            if edge is not None:
                days.update({edge - timedelta(days=1), edge, edge + timedelta(days=1)})
        for relation in note.relations:
            for edge in (relation.valid_from, relation.valid_to):
                if edge is not None:
                    days.update({edge - timedelta(days=1), edge, edge + timedelta(days=1)})
    ordered = sorted(days)
    return ordered[:: max(1, len(ordered) // 8)]


def _terms(notes: list[Note]) -> list[list[str]]:
    """Queries drawn from the notes themselves, plus ones that match nothing or everything."""
    drawn = [query_terms(note.body)[:3] for note in notes[:25]]
    tags = [note.tags[:2] for note in notes if note.tags][:5]
    return [terms for terms in [*drawn, *tags] if terms] + [["zzzz-not-there"], ["e"], ["pd", "ee"]]


async def _same_answers(left: NoteStore, right: NoteStore, notes: list[Note]) -> None:
    """Every question of `NoteStore`, asked of both stores about every node, answered equally."""
    assert {n.id: n for n in await left.notes()} == {n.id: n for n in await right.notes()}
    ids = sorted(note.id for note in notes)
    graph = await left.graph()
    other = await right.graph()
    assert set(graph.nodes) == set(other.nodes)
    assert {
        (src, dst): sorted(map(repr, data["relations"]))
        for src, dst, data in graph.edges(data=True)
    } == {
        (src, dst): sorted(map(repr, data["relations"]))
        for src, dst, data in other.edges(data=True)
    }
    assert await left.get_many([*ids, "no-such-note"]) == await right.get_many(
        [*ids, "no-such-note"]
    )
    days = _boundaries(notes)
    relations = sorted({r.rel for note in notes for r in note.outgoing_relations()})
    for node in sorted(graph.nodes):
        assert _by_link(await left.edges(node)) == _by_link(await right.edges(node)), node
        for hops in range(5):
            assert await _outcome(left.neighborhood(node, hops)) == await _outcome(
                right.neighborhood(node, hops)
            ), (node, hops)
        for rel in relations:
            for as_of in (None, *days):
                assert await _outcome(left.related(node, rel, as_of)) == await _outcome(
                    right.related(node, rel, as_of)
                ), (node, rel, as_of)
        for as_of in days:
            assert await left.current_successor(node, as_of) == await right.current_successor(
                node, as_of
            ), (node, as_of)
    for terms in _terms(notes):
        for as_of in days:
            assert await left.search(terms, as_of) == await right.search(terms, as_of), (
                terms,
                as_of,
            )


async def test_the_committed_corpus_is_answered_alike_by_both_stores(
    store: PostgresNoteStore,
) -> None:
    """The 40-odd notes under `knowledge/`, read from files by one store and from rows by the other.

    The Postgres store is filled through its own write, from the parsed notes; the git store reads
    the directory as it is. A hand-formatted file and its canonical render are one note.
    """
    notes = load_notes(_SHIPPED)
    assert len(notes) >= 40
    for note in notes:
        await store.put(NoteUnit(note))
    await _same_answers(GitNoteStore(_SHIPPED), store, notes)


async def test_a_note_has_the_digest_of_its_canonical_render_whichever_store_holds_it(
    store: PostgresNoteStore,
) -> None:
    """A note has one digest, so the corpus fingerprints are the notes' own."""
    notes = load_notes(_SHIPPED)
    for note in notes:
        await store.put(NoteUnit(note))
    assert await store.fingerprints() == {note.id: content_sha(note) for note in notes}


_WORDS = ["suzuki", "solvent", "ester", "base", "pd", "thf", "yield", "impurity", "screen", "dmf"]
_RELATIONS = ["cites", "uses", "supersedes", "superseded-by", "precursor-of", "contradicts"]


def _synthetic(seed: int, size: int = 36) -> list[Note]:
    """A random graph: links to notes, to nothing, to themselves; windows; retirement chains.

    Targets are drawn from the ids, from ids no note defines and from `reaction-` records, so
    cycles, dangling nodes and self-links all occur; roughly a third of the notes are retired or
    not yet current.
    """
    rng = random.Random(seed)
    ids = [f"n{i:02d}" for i in range(size)]
    targets = [*ids, *(f"ghost-{i}" for i in range(6)), "reaction-eln.7", "reaction-9"]
    start = date(2026, 1, 1)

    def day() -> date:
        return start + timedelta(days=rng.randrange(0, 300))

    notes: list[Note] = []
    for note_id in ids:
        links = [
            f"[[{rng.choice(_RELATIONS)}:{rng.choice(targets)}]]"
            if rng.random() < 0.5
            else f"[[{rng.choice(targets)}]]"
            for _ in range(rng.randrange(0, 4))
        ]
        relations = []
        for _ in range(rng.randrange(0, 3)):
            first = day() if rng.random() < 0.5 else None
            last = (
                first + timedelta(days=rng.randrange(0, 120))
                if first and rng.random() < 0.7
                else None
            )
            relations.append(
                Relation(
                    rel=rng.choice(_RELATIONS),
                    to=rng.choice(targets),
                    valid_from=first,
                    valid_to=last,
                    confidence=rng.choice([None, 0.5]),
                )
            )
        window_start = day() if rng.random() < 0.3 else None
        window_end = (
            (window_start or start) + timedelta(days=rng.randrange(0, 200))
            if rng.random() < 0.4
            else None
        )
        notes.append(
            Note(
                id=note_id,
                type=rng.choice(["reaction", "compound", "playbook"]),
                created_by="agent",
                tags=rng.sample(_WORDS, rng.randrange(0, 3)),
                valid_from=window_start,
                valid_to=window_end,
                relations=relations,
                body=" ".join([*rng.sample(_WORDS, 4), *links]),
            )
        )
    return notes


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
async def test_synthetic_graphs_are_answered_alike_by_both_stores(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cycles, self-links, dangling targets, retired notes and windows, question by question."""
    notes = _synthetic(seed)
    git: Harness = await STORES["git"](tmp_path, monkeypatch)
    postgres: Harness = await STORES["postgres"](tmp_path, monkeypatch)
    await git.add(*notes)
    await postgres.add(*notes)
    await _same_answers(git.store, postgres.store, notes)


async def test_the_order_notes_arrive_in_changes_no_answer(store: PostgresNoteStore) -> None:
    """Edges, neighbours and successors are ordered by id, not by when a row was written.

    The same graph in two tenants, one written in id order and one shuffled.
    """
    notes = _synthetic(7)
    shuffled = PostgresNoteStore("shuffled")
    for note in notes:
        await store.put(NoteUnit(note))
    for note in random.Random(3).sample(notes, len(notes)):
        await shuffled.put(NoteUnit(note))
    await _same_answers(store, shuffled, notes)
    edges = await store.edges("n03")
    assert edges == await store.edges("n03") == await shuffled.edges("n03")
    links = [(edge.src, edge.dst) for edge in edges]
    outgoing = [link for link in links if link[0] == "n03"]
    assert outgoing == sorted(outgoing) and links[: len(outgoing)] == outgoing


# --- A note is written in one transaction -------------------------------------------------------


async def test_a_first_write_is_revision_one_and_an_edit_is_the_next_and_both_are_kept(
    store: PostgresNoteStore,
) -> None:
    """The row holds the latest; the history holds every revision, edges follow the latest."""
    first = await store.put(NoteUnit(_note("subject", "See [[old-link]].")))
    assert (first.revisions, first.written, first.notes) == ({"subject": 1}, ("subject",), 1)
    created = await _rows("SELECT created_at, updated_at FROM kg_notes WHERE id = 'subject'")
    second = await store.put(NoteUnit(_note("subject", "See [[new-link]].", tags=["edited"])))
    assert (second.revisions, second.written) == ({"subject": 2}, ("subject",))
    (row,) = await _rows(
        "SELECT revision, body, search_text, created_at, updated_at FROM kg_notes "
        "WHERE id = 'subject'"
    )
    assert row[0] == 2 and row[1] == "See [[new-link]]."
    assert "edited" in row[2] and "old-link" not in row[2]
    assert row[3] == created[0][0] and row[4] >= created[0][1]
    history = await _rows(
        "SELECT revision, body FROM kg_note_revisions WHERE note_id = 'subject' ORDER BY revision"
    )
    assert history == [(1, "See [[old-link]]."), (2, "See [[new-link]].")]
    assert await _rows("SELECT dst FROM kg_edges WHERE src = 'subject'") == [("new-link",)]


async def test_writing_identical_content_again_adds_no_revision_and_moves_nothing(
    store: PostgresNoteStore,
) -> None:
    """An idempotent repeat is a no-op a cache can see: same signature and token."""
    note = _note("steady", "Same words. [[beta]]")
    await store.put(NoteUnit(note))
    token, _ = await store.snapshot()
    signature = await store.signature()
    again = await store.put(NoteUnit(note))
    assert (again.written, again.notes, again.revisions) == ((), 0, {"steady": 1})
    assert await store.signature() == signature and (await store.snapshot())[0] == token
    assert await _rows("SELECT count(*) FROM kg_note_revisions") == [(1,)]
    await store.put(NoteUnit(note.model_copy(update={"body": "Other words."})))
    assert await store.signature() != signature and (await store.snapshot())[0] != token


async def test_a_note_is_stored_redacted_and_its_digest_is_of_what_is_stored(
    store: PostgresNoteStore,
) -> None:
    """A credential in a body does not reach the table or the history; the digest describes both."""
    secret = "postgresql://admin:hunter2secret@db.internal/chem"
    note = _note("leaky", f"Connect with {secret} to read it.")
    await store.put(NoteUnit(note))
    stored = await store.get("leaky")
    assert stored is not None and "hunter2secret" not in stored.body
    for table, column in (("kg_notes", "id"), ("kg_note_revisions", "note_id")):
        (row,) = await _rows(f"SELECT body, content_sha FROM {table} WHERE {column} = 'leaky'")
        assert "hunter2secret" not in row[0] and row[1] == content_sha(note)
    canonical = redact_secrets(render_note(note))
    assert (await store.fingerprints())["leaky"] == (
        f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    )


async def test_a_unit_is_written_dependencies_then_subject_then_retirements_in_one_commit(
    store: PostgresNoteStore,
) -> None:
    """Three roles, three outcomes: a new dependency, the subject, and the retired note's close."""
    old = _note("old-entry", "The first entry.")
    await store.put(NoteUnit(old))
    retired = old.model_copy(
        update={
            "valid_to": date(2026, 1, 31),
            "relations": [Relation(rel="superseded-by", to="new-entry")],
        }
    )
    unit = NoteUnit(
        subject=_note("new-entry", "Cites [[compound-x]] and replaces [[old-entry]]."),
        dependencies=(_note("compound-x", "A compound."),),
        retirements=(retired,),
    )
    assert [(role, n.id) for role, n in unit.ordered()] == [
        ("dependency", "compound-x"),
        ("subject", "new-entry"),
        ("retirement", "old-entry"),
    ]
    outcome = await store.put(unit)
    assert outcome.written == ("compound-x", "new-entry", "old-entry")
    assert outcome.revisions == {"compound-x": 1, "new-entry": 1, "old-entry": 2}
    assert (await store.current_successor("old-entry", date(2026, 3, 1))) is not None
    # One transaction, so one commit time for the three revisions.
    assert await _rows(
        "SELECT count(DISTINCT recorded_at) FROM kg_note_revisions WHERE "
        "(note_id, revision) IN (('compound-x', 1), ('new-entry', 1), ('old-entry', 2))"
    ) == [(1,)]


async def test_a_unit_that_fails_leaves_none_of_its_notes_behind(store: PostgresNoteStore) -> None:
    """A refused subject takes the dependencies and retirements written before it back with it."""
    await store.put(NoteUnit(_note("held", "Version one.")))
    unit = NoteUnit(
        subject=_note("held", "Version two."),
        dependencies=(_note("first-dependency", "Would land first."),),
        expected_revision=5,
    )
    with pytest.raises(NoteRevisionConflict) as caught:
        await store.put(unit)
    assert (caught.value.note_id, caught.value.expected, caught.value.actual) == ("held", 5, 1)
    assert await store.get("first-dependency") is None
    held = await store.get("held")
    assert held is not None and held.body == "Version one."
    assert await _rows("SELECT count(*) FROM kg_note_revisions") == [(1,)]


async def test_a_dependency_that_is_already_there_is_left_exactly_as_it_is(
    store: PostgresNoteStore,
) -> None:
    """A dependency is re-rendered from source data on every write; an edit by a person survives."""
    await store.put(NoteUnit(_note("compound-y", "Edited by hand.", tags=["hazard"])))
    outcome = await store.put(
        NoteUnit(
            _note("user", "Cites [[compound-y]]."), dependencies=(_note("compound-y", "Machine."),)
        )
    )
    assert outcome.written == ("user",) and outcome.revisions["compound-y"] == 1
    kept = await store.get("compound-y")
    assert kept is not None and kept.body == "Edited by hand." and kept.tags == ["hazard"]


# --- A person's note ----------------------------------------------------------------------------


async def test_an_agent_subject_may_not_take_a_humans_note_and_the_unit_is_refused_whole(
    store: PostgresNoteStore,
) -> None:
    """The refusal comes after the dependency was written, so it also proves the rollback."""
    person = Note(id="curated", type="playbook", created_by="human", body="By a person.")
    await store.put(NoteUnit(person))
    with pytest.raises(HumanNoteProtected):
        await store.put(
            NoteUnit(_note("curated", "Forged."), dependencies=(_note("side-effect", "x"),))
        )
    assert await store.get("side-effect") is None
    assert await store.get("curated") == person


async def test_a_retirement_of_a_humans_note_is_left_alone_and_the_subject_still_lands(
    store: PostgresNoteStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The person's file is untouched, as the file writer leaves it, and the new note lands."""
    person = Note(id="curated", type="playbook", created_by="human", body="By a person.")
    await store.put(NoteUnit(person))
    closed = person.model_copy(update={"valid_to": date(2026, 1, 1)})
    with caplog.at_level(logging.WARNING):
        outcome = await store.put(
            NoteUnit(_note("successor", "Replaces [[curated]]."), retirements=(closed,))
        )
    assert outcome.written == ("successor",) and outcome.revisions["curated"] == 1
    assert await store.get("curated") == person
    assert "left curated alone" in caplog.text


async def test_a_person_may_edit_any_note_and_it_becomes_theirs(store: PostgresNoteStore) -> None:
    """The refusal is of an agent over a person, not of a revision over a note."""
    await store.put(NoteUnit(_note("shared", "Written by the agent.")))
    edited = Note(id="shared", type="reaction", created_by="human", body="Corrected by a chemist.")
    outcome = await store.put(NoteUnit(edited, expected_revision=1))
    assert outcome.revisions == {"shared": 2}
    assert await store.get("shared") == edited


# --- Optimistic concurrency ---------------------------------------------------------------------


async def test_a_write_names_the_revision_it_read_and_a_stale_one_is_refused(
    store: PostgresNoteStore,
) -> None:
    """`0` means new, `n` means revision `n`; the error says what the store holds."""
    note = _note("claimed", "One.")
    await store.put(NoteUnit(note, expected_revision=0))
    with pytest.raises(NoteRevisionConflict) as already:
        await store.put(NoteUnit(note.model_copy(update={"body": "Two."}), expected_revision=0))
    assert (already.value.expected, already.value.actual) == (0, 1)
    await store.put(NoteUnit(note.model_copy(update={"body": "Two."}), expected_revision=1))
    with pytest.raises(NoteRevisionConflict) as stale:
        await store.put(NoteUnit(note.model_copy(update={"body": "Three."}), expected_revision=1))
    assert (stale.value.expected, stale.value.actual) == (1, 2)
    unwritten = await store.put(NoteUnit(note.model_copy(update={"body": "Four."})))
    assert unwritten.revisions == {"claimed": 3}


async def test_of_writers_that_read_the_same_revision_exactly_one_wins(
    store: PostgresNoteStore,
) -> None:
    """Eight writers claim a new id, then eight edit revision 1: one succeeds each time."""
    results = await asyncio.gather(
        *(
            store.put(NoteUnit(_note("race", f"Writer {i}."), expected_revision=0))
            for i in range(8)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(isinstance(r, NoteRevisionConflict) for r in results if isinstance(r, BaseException))
    results = await asyncio.gather(
        *(store.put(NoteUnit(_note("race", f"Edit {i}."), expected_revision=1)) for i in range(8)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert await _rows("SELECT revision FROM kg_notes WHERE id = 'race'") == [(2,)]
    assert await _rows("SELECT count(*) FROM kg_note_revisions WHERE note_id = 'race'") == [(2,)]


async def test_writers_whose_units_overlap_in_opposite_orders_neither_deadlock_nor_lose_a_revision(
    store: PostgresNoteStore,
) -> None:
    """Unit A writes x then y, unit B y then x; locks go in id order, not write order."""

    async def writer(first: str, second: str, rounds: int, tag: str) -> None:
        for i in range(rounds):
            await store.put(
                NoteUnit(_note(second, f"{tag} {i}"), retirements=(_note(first, f"{tag} {i}"),))
            )

    await store.put(NoteUnit(_note("x", "seed")))
    await store.put(NoteUnit(_note("y", "seed")))
    await asyncio.wait_for(
        asyncio.gather(
            writer("x", "y", 12, "a"), writer("y", "x", 12, "b"), writer("x", "y", 12, "c")
        ),
        timeout=60,
    )
    for note_id in ("x", "y"):
        revisions = [
            r[0]
            for r in await _rows(
                "SELECT revision FROM kg_note_revisions WHERE note_id = %s ORDER BY revision",
                (note_id,),
            )
        ]
        assert revisions == list(range(1, len(revisions) + 1)), note_id
        assert await _rows("SELECT revision FROM kg_notes WHERE id = %s", (note_id,)) == [
            (revisions[-1],)
        ]
        # The row holds what the newest revision recorded.
        assert await _rows(
            "SELECT n.body = r.body FROM kg_notes n JOIN kg_note_revisions r "
            "ON r.note_id = n.id AND r.revision = n.revision WHERE n.id = %s",
            (note_id,),
        ) == [(True,)]


async def test_four_writers_on_distinct_notes_write_every_note_once(
    store: PostgresNoteStore,
) -> None:
    """The shape of the throughput case: writers that do not overlap never wait on each other."""

    async def writer(index: int) -> None:
        for i in range(10):
            await store.put(NoteUnit(_note(f"w{index}-{i}", f"Note {i} of writer {index}.")))

    await asyncio.gather(*(writer(i) for i in range(4)))
    assert await _rows("SELECT count(*), sum(revision) FROM kg_notes") == [(40, 40)]
    assert await _rows("SELECT count(*) FROM kg_note_revisions") == [(40,)]


# --- Who, when, whose ---------------------------------------------------------------------------


async def test_a_revision_records_the_person_whose_turn_wrote_it(store: PostgresNoteStore) -> None:
    """`recorded_by` is the bound actor, and null where nobody is bound."""
    await store.put(NoteUnit(_note("anon", "No one bound.")))
    tokens = set_current_identity("chemist-1", frozenset())
    try:
        await store.put(NoteUnit(_note("anon", "Now someone is.", actor="chemist-1")))
    finally:
        reset_current_identity(tokens)
    assert await _rows(
        "SELECT revision, recorded_by FROM kg_note_revisions WHERE note_id = 'anon' ORDER BY 1"
    ) == [(1, None), (2, "chemist-1")]


async def test_tenants_see_only_their_own_notes_and_may_reuse_an_id(
    store: PostgresNoteStore,
) -> None:
    """Every statement names the tenant: the same id is two notes, and edges stay with their own."""
    other = PostgresNoteStore("other")
    await store.put(NoteUnit(_note("same-id", "Default tenant. [[target]]")))
    await other.put(NoteUnit(_note("same-id", "Other tenant. [[elsewhere]]")))
    await other.put(NoteUnit(_note("only-other", "Mine.")))
    assert [n.id for n in await store.notes()] == ["same-id"]
    assert sorted(n.id for n in await other.notes()) == ["only-other", "same-id"]
    got = await store.get("same-id")
    assert got is not None and got.body.startswith("Default")
    assert await store.related("same-id", "cites") == ["target"]
    assert await other.related("same-id", "cites") == ["elsewhere"]
    assert await store.get("only-other") is None
    assert await store.neighborhood("same-id", 2) == {"target"}
    assert (await store.signature())[0] == 1 and (await other.signature())[0] == 2


# --- Reading what is not a valid note -----------------------------------------------------------


async def test_a_row_that_fails_the_schema_is_skipped_by_readers_and_named_by_unparseable(
    store: PostgresNoteStore,
) -> None:
    """One bad row cannot block a query, as one bad file cannot; the audit names it."""
    await store.put(NoteUnit(_note("good", "Fine.")))
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO kg_notes (id, type, frontmatter, body, content_sha) "
            "VALUES ('bad', 'reaction', '{\"surprise\": 1}', 'x', 'sha256:0')"
        )
        await conn.commit()
    assert [n.id for n in await store.notes()] == ["good"]
    assert await store.get("bad") is None
    assert (await store.get_many(["good", "bad"]))["bad"] is None
    assert await store.unparseable(["good", "bad", "absent"]) == ["bad"]
    assert [n.id for n in (await store.snapshot())[1]] == ["good"]


# --- The queries are served by the indexes ------------------------------------------------------


async def test_the_edge_lookups_and_the_substring_search_use_their_indexes() -> None:
    """Out-edges by the primary key, in-edges by `kg_edges_dst_idx`, a term by the trigram index."""
    edges = (
        "INSERT INTO kg_edges (src, rel, dst) SELECT 'n' || i, 'cites', 'n' || (i + 1) "
        "FROM generate_series(1, 3000) AS i"
    )
    assert (
        await planned_index(
            "kg_edges",
            edges,
            "SELECT dst FROM kg_edges WHERE tenant_id = %s AND src = %s AND rel = %s",
            ("default", "n5", "cites"),
        )
        == "kg_edges_pkey"
    )
    assert (
        await planned_index(
            "kg_edges",
            edges,
            "SELECT src FROM kg_edges WHERE tenant_id = %s AND dst = %s",
            ("default", "n5"),
        )
        == "kg_edges_dst_idx"
    )
    notes = (
        "INSERT INTO kg_notes (id, type, content_sha, search_text) "
        "SELECT 'n' || i, 'reaction', 'sha256:0', 'note number ' || i || ' about solvents' "
        "FROM generate_series(1, 3000) AS i"
    )
    assert (
        await planned_index(
            "kg_notes",
            notes,
            "SELECT id FROM kg_notes WHERE tenant_id = %s AND search_text LIKE %s",
            ("default", "%number 77%"),
        )
        == "kg_notes_search_text_trgm_idx"
    )


# --- The unit is the order the file writer uses -------------------------------------------------


def test_a_unit_orders_its_notes_as_the_file_write_does() -> None:
    """One definition of the order: `record._build_write` builds its files from `NoteUnit`."""
    subject = _note("subject")
    dependency = _note("dependency")
    retired = _note("retired")
    cases = [
        ([dependency, subject, retired], [retired, dependency, subject]),
        ([], [subject]),
        ([_note("subject")], [dependency]),
    ]
    for dependencies, retirements in cases:
        unit = NoteUnit(subject, tuple(dependencies), tuple(retirements))
        write = _build_write(subject, "knowledge", dependencies, retirements)
        assert [f"knowledge/reaction/{n.id}.md" for _, n in unit.ordered()] == [
            f.path for f in write.files
        ]
        assert [(f.overwrite, f.amendment) for f in write.files] == [
            (role != "dependency", role == "retirement") for role, _ in unit.ordered()
        ]
