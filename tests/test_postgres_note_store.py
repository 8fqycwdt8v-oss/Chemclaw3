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
from collections.abc import AsyncIterator, Awaitable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect, pooling
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import redact_secrets
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note, Relation, note_relative_path
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
    NoteWriteError,
)
from tests.note_store_harness import STORES, Harness, empty_note_tables
from tests.pg import planned_index

_SHIPPED = Path(__file__).resolve().parents[1] / "knowledge"


@pytest.fixture(autouse=True)
async def _pooled() -> AsyncIterator[None]:
    """Pool connections as the front door does, so a question costs a query and not a connect."""
    async with pooling():
        yield


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


def _around(days: Iterable[date | None]) -> set[date]:
    """Each day and its neighbours, where an off-by-one in an inclusive range would show."""
    return {
        day + timedelta(days=offset) for day in days if day is not None for offset in (-1, 0, 1)
    }


_GENERIC_DAYS = [date(2020, 1, 1), date(2026, 10, 15), date(2035, 1, 1)]


def _boundaries(notes: list[Note]) -> list[date]:
    """Every day on or beside a window edge of any note or relation, and a few far from any."""
    edges: list[date | None] = []
    for note in notes:
        edges += [note.valid_from, note.valid_to]
        for relation in note.outgoing_relations():
            edges += [relation.valid_from, relation.valid_to]
    return sorted(_around(edges) | set(_GENERIC_DAYS))


def _own_days(note: Note | None) -> list[date]:
    """The days beside the window edges of one note's own relations, the ones `related` reads."""
    if note is None:
        return _GENERIC_DAYS
    edges = [d for r in note.outgoing_relations() for d in (r.valid_from, r.valid_to)]
    return sorted(_around(edges) | set(_GENERIC_DAYS))


def _terms(notes: list[Note]) -> list[list[str]]:
    """Queries drawn from the notes themselves, plus ones that match nothing or everything."""
    drawn = [query_terms(note.body)[:3] for note in notes[:25]]
    tags = [note.tags[:2] for note in notes if note.tags][:5]
    return [terms for terms in [*drawn, *tags] if terms] + [["zzzz-not-there"], ["e"], ["pd", "ee"]]


async def _same_answers(left: NoteStore, right: NoteStore, notes: list[Note]) -> None:
    """Every question of `NoteStore`, asked of both stores about every node, answered equally.

    Compared as the contract states them: sets, sorted lists, and search hits in id order. The
    order in which a store lists a link's relations, or the notes of `notes()`, is its own (the
    git store's is insertion order, this one's is by id) and is not part of the contract; that each
    store's own order is stable is tested separately.

    `related` is asked on every day beside its node's own edge windows, `current_successor` on
    every window edge in the corpus for the nodes on a supersede link, and `search` on every such
    day for three queries, so a range that is off by a day at any edge is found.
    """
    assert {n.id: n for n in await left.notes()} == {n.id: n for n in await right.notes()}
    ids = sorted(note.id for note in notes)
    by_id = {note.id: note for note in notes}
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
        for hops in range(4):
            assert await _outcome(left.neighborhood(node, hops)) == await _outcome(
                right.neighborhood(node, hops)
            ), (node, hops)
        own = by_id.get(node)
        asked = {r.rel for r in own.outgoing_relations()} if own else set()
        for rel in sorted(asked) + [r for r in relations if r not in asked][:1]:
            for as_of in (None, *_own_days(own)):
                assert await _outcome(left.related(node, rel, as_of)) == await _outcome(
                    right.related(node, rel, as_of)
                ), (node, rel, as_of)
        on_a_chain = any(
            r.rel in {"supersedes", "superseded-by"}
            for edge in await left.edges(node)
            for r in edge.relations
        )
        for as_of in days if on_a_chain else _GENERIC_DAYS:
            assert await left.current_successor(node, as_of) == await right.current_successor(
                node, as_of
            ), (node, as_of)
    queries = _terms(notes)
    spread = days[:: max(1, len(days) // 4)]
    for index, terms in enumerate(queries):
        for as_of in days if index < 3 else spread:
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
    # One transaction: the three revisions were written by one transaction id.
    assert await _rows(
        "SELECT count(DISTINCT xmin::text) FROM kg_note_revisions WHERE "
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


_DAY = date(2026, 10, 15)


async def _exec(sql: str, params: Iterable[Any] = ()) -> None:
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute(sql, tuple(params))
        await conn.commit()


async def _two_tenants(store: PostgresNoteStore) -> PostgresNoteStore:
    """The same ids in two tenants, with different content, and a link target both cite."""
    other = PostgresNoteStore("other")
    retired = {"valid_to": date(2026, 1, 1)}
    relations = [Relation(rel="superseded-by", to="p2")]
    for tenant_store, notes in (
        (
            store,
            [
                _note("p", "alpha unique-a [[shared]]", relations=relations, **retired),
                _note("p2", "alpha successor unique-a2 [[q]]"),
                _note("q", "quiet note"),
            ],
        ),
        (
            other,
            [
                _note("p", "beta unique-b [[shared]] [[extra-b]]", relations=relations, **retired),
                _note("p2", "beta successor [[q]]", valid_to=date(2026, 2, 1)),
                _note("q", "beta quiet [[shared]]"),
                _note("only-b", "only in b"),
                _note("only-b2", "also only in b"),
            ],
        ),
    ):
        for note in notes:
            await tenant_store.put(NoteUnit(note))
    await _exec(
        "INSERT INTO kg_notes (tenant_id, id, type, frontmatter, body, content_sha, valid_from, "
        "valid_to) VALUES ('other', 'bad', 'reaction', '{\"surprise\": 1}', 'x', 'sha256:0', "
        "'2020-01-01', '2020-01-01')"
    )
    return other


async def test_every_read_names_its_tenant_when_the_same_ids_and_a_link_target_are_shared(
    store: PostgresNoteStore,
) -> None:
    """Two tenants hold `p`, `p2`, `q` and cite `shared`; each question is answered from one."""
    other = await _two_tenants(store)

    async def ids(notes: Awaitable[list[Note]]) -> list[str]:
        return [n.id for n in await notes]

    assert await ids(store.notes()) == ["p", "p2", "q"]
    assert await ids(other.notes()) == ["only-b", "only-b2", "p", "p2", "q"]
    got, got_other = await store.get("p"), await other.get("p")
    assert got is not None and got.body.startswith("alpha")
    assert got_other is not None and got_other.body.startswith("beta")
    assert await store.get("only-b") is None and await other.get("only-b") is not None
    mine, theirs = await store.get_many(["p", "only-b"]), await other.get_many(["p", "only-b"])
    assert mine["only-b"] is None and theirs["only-b"] is not None
    assert await store.related("p", "cites") == ["shared"]
    assert await other.related("p", "cites") == ["extra-b", "shared"]
    assert await store.related("p", "cites", _DAY) == ["shared"]
    assert await other.related("p", "cites", _DAY) == ["extra-b", "shared"]
    # `shared` is cited by one note here and two there, so both arms of the walk are exercised.
    assert await store.neighborhood("shared", 1) == {"p"}
    assert await other.neighborhood("shared", 1) == {"p", "q"}
    assert await store.neighborhood("p", 2) == {"shared", "p2", "q"}
    assert await other.neighborhood("p", 2) == {"shared", "extra-b", "p2", "q"}
    with pytest.raises(KeyError):
        await store.neighborhood("only-b")
    # A link target only the other tenant cites is no node here.
    with pytest.raises(KeyError):
        await store.neighborhood("extra-b")
    assert await other.neighborhood("extra-b") == {"p"}
    assert sorted((e.src, e.dst) for e in await store.edges("shared")) == [("p", "shared")]
    assert sorted((e.src, e.dst) for e in await other.edges("shared")) == [
        ("p", "shared"),
        ("q", "shared"),
    ]
    assert await store.edges("only-b") == []
    successor = await store.current_successor("p", _DAY)
    assert successor is not None and successor.body.startswith("alpha")
    assert await other.current_successor("p", _DAY) is None
    batch = await store.current_successors(["p", "q", "only-b"], _DAY)
    assert batch["p"] is not None and batch["q"] is None and batch["only-b"] is None
    assert set((await other.current_successors(["p", "q"], _DAY)).values()) == {None}
    own = await store.search(["unique-a"], _DAY)
    assert (own.searched, [(c, n.id) for c, n in own.matches]) == (2, [(1, "p2")])
    their_search = await other.search(["unique-a"], _DAY)
    assert (their_search.searched, their_search.matches) == (3, [])
    beta = await other.search(["beta"], _DAY)
    assert [(c, n.id) for c, n in beta.matches] == [(1, "q")]
    assert (await store.search(["beta"], _DAY)).matches == []
    mine_graph, their_graph = await store.graph(), await other.graph()
    assert set(mine_graph.nodes) == {"p", "p2", "q", "shared"}
    assert set(their_graph.nodes) == {"p", "p2", "q", "only-b", "only-b2", "shared", "extra-b"}
    assert set(mine_graph.successors("p")) == {"shared", "p2"}
    token, snapshot = await store.snapshot()
    assert [n.id for n in snapshot] == ["p", "p2", "q"] and token is not None
    assert (await store.signature(), await other.signature()) == ((3, 3), (6, 6))  # `bad` counts
    assert set(await store.arrivals()) == {"p", "p2", "q"}
    assert set(await other.arrivals()) == {"bad", "only-b", "only-b2", "p", "p2", "q"}
    assert set(await store.fingerprints()) == {"p", "p2", "q"}
    assert set(await other.fingerprints()) == {"bad", "only-b", "only-b2", "p", "p2", "q"}
    assert await store.unparseable(["bad", "q"]) == []
    assert await other.unparseable(["bad", "q"]) == ["bad"]


async def test_a_supersede_link_in_another_tenant_is_not_followed(
    store: PostgresNoteStore,
) -> None:
    """`new-x supersedes old-x` in one tenant says nothing about `old-x` in the other."""
    other = PostgresNoteStore("other")
    retired = {"valid_to": date(2026, 1, 1)}
    for tenant_store in (store, other):
        await tenant_store.put(NoteUnit(_note("old-x", "Retired.", **retired)))
    await store.put(NoteUnit(_note("new-x", "Unrelated here.")))
    await other.put(
        NoteUnit(_note("new-x", "Replaces it.", relations=[Relation(rel="supersedes", to="old-x")]))
    )
    assert await store.current_successor("old-x", _DAY) is None
    assert (await store.current_successors(["old-x"], _DAY))["old-x"] is None
    successor = await other.current_successor("old-x", _DAY)
    assert successor is not None and successor.id == "new-x"


async def test_every_write_touches_only_its_tenant(store: PostgresNoteStore) -> None:
    """A revision, a retirement and a repair of derived data leave the other tenant's rows alone."""
    other = await _two_tenants(store)
    snapshots = (
        "SELECT src, rel, dst FROM kg_edges WHERE tenant_id = 'other' ORDER BY 1, 2, 3",
        "SELECT id, revision, body, valid_to, search_text FROM kg_notes "
        "WHERE tenant_id = 'other' ORDER BY 1",
        "SELECT note_id, revision FROM kg_note_revisions WHERE tenant_id = 'other' ORDER BY 1, 2",
    )
    before = [await _rows(sql) for sql in snapshots]
    # A revision of `p` that drops its links deletes this tenant's edges of `p` and no other's.
    await store.put(NoteUnit(_note("p", "alpha without links", valid_to=date(2026, 1, 1))))
    # A retirement closes this tenant's `q`.
    await store.put(
        NoteUnit(
            _note("q-successor", "Replaces [[q]]."),
            retirements=(_note("q", "quiet note", valid_to=date(2026, 3, 1)),),
        )
    )
    # Derived data damaged in this tenant only: an identical write repairs it, and only here.
    mine = await store.get("p2")
    assert mine is not None
    await _exec(
        "UPDATE kg_notes SET search_text = 'damaged' WHERE id = 'p2' AND tenant_id = 'default'"
    )
    await _exec("DELETE FROM kg_edges WHERE src = 'p2' AND tenant_id = 'default'")
    await store.put(NoteUnit(mine))
    assert [await _rows(sql) for sql in snapshots] == before
    assert await store.related("p2", "cites") == ["q"]
    assert [n.id for _, n in (await store.search(["unique-a2"], _DAY)).matches] == ["p2"]
    assert await store.related("p", "cites") == []
    assert await other.related("p", "cites") == ["extra-b", "shared"]
    closed, kept = await store.get("q"), await other.get("q")
    assert closed is not None and closed.valid_to == date(2026, 3, 1)
    assert kept is not None and kept.valid_to is None


async def test_an_identical_write_repairs_what_is_derived_from_the_content_and_adds_no_revision(
    store: PostgresNoteStore,
) -> None:
    """A row without its haystack, an edge that was lost and one that does not belong: put again."""
    note = _note("derived", "Mentions suzuki. [[beta]]")
    await store.put(NoteUnit(note))
    await _exec("UPDATE kg_notes SET search_text = '' WHERE id = 'derived'")
    await _exec("DELETE FROM kg_edges WHERE src = 'derived'")
    await _exec("INSERT INTO kg_edges (src, rel, dst) VALUES ('derived', 'cites', 'stray')")
    assert (await store.search(["suzuki"], _DAY)).matches == []
    assert await store.related("derived", "cites") == ["stray"]
    signature = await store.signature()
    outcome = await store.put(NoteUnit(note))
    assert (outcome.written, outcome.revisions) == ((), {"derived": 1})
    assert [n.id for _, n in (await store.search(["suzuki"], _DAY)).matches] == ["derived"]
    assert await store.related("derived", "cites") == ["beta"]
    assert await store.signature() == signature
    assert await _rows("SELECT count(*) FROM kg_note_revisions") == [(1,)]
    # With nothing to repair, a repeat touches nothing.
    (stamp,) = await _rows("SELECT updated_at FROM kg_notes WHERE id = 'derived'")
    await store.put(NoteUnit(note))
    assert await _rows("SELECT updated_at FROM kg_notes WHERE id = 'derived'") == [stamp]


async def test_a_revision_is_stamped_after_the_wait_for_its_lock_and_never_runs_backwards(
    store: PostgresNoteStore,
) -> None:
    """A writer that begins early and waits behind another is stamped when it writes, not begins."""
    await store.put(NoteUnit(_note("queued", "v1")))
    async with await connect(settings.postgres_dsn) as holder:
        await holder.execute("SELECT pg_advisory_xact_lock(hashtextextended('default:queued', 0))")
        cursor = await holder.execute("SELECT clock_timestamp()")
        row = await cursor.fetchone()
        assert row is not None
        before = row[0]
        waiting = asyncio.create_task(store.put(NoteUnit(_note("queued", "v2"))))
        await asyncio.sleep(2.0)
        assert not waiting.done(), "the writer should be queued behind the lock"
        await holder.commit()
    await waiting
    stamps = await _rows(
        "SELECT recorded_at FROM kg_note_revisions WHERE note_id = 'queued' ORDER BY revision"
    )
    assert stamps[1][0] >= before + timedelta(seconds=1.5)
    assert stamps[1][0] > stamps[0][0]
    assert await _rows("SELECT updated_at >= %s FROM kg_notes WHERE id = 'queued'", (before,)) == [
        (True,)
    ]


async def test_a_retirement_closes_the_note_as_it_now_stands_and_keeps_an_edit_made_since(
    store: PostgresNoteStore,
) -> None:
    """The copy a retirement carries was read earlier; applying only the window keeps the edit."""
    await store.put(NoteUnit(_note("target", "Original.")))
    read = await store.get("target")
    assert read is not None
    retirement = read.model_copy(
        update={
            "valid_to": date(2026, 4, 30),
            "relations": [Relation(rel="superseded-by", to="successor")],
            "body": "Original.\n\nSuperseded by successor.",
        }
    )
    await store.put(NoteUnit(_note("target", "Edited meanwhile.", tags=["edited"])))
    outcome = await store.put(
        NoteUnit(_note("successor", "The new one."), retirements=(retirement,))
    )
    assert outcome.revisions == {"successor": 1, "target": 3}
    now = await store.get("target")
    assert now is not None
    assert (now.body, now.tags, now.valid_to) == (
        "Edited meanwhile.",
        ["edited"],
        date(2026, 4, 30),
    )
    assert await store.related("target", "superseded-by") == ["successor"]
    history = await _rows(
        "SELECT revision, body, valid_to FROM kg_note_revisions WHERE note_id = 'target' "
        "ORDER BY revision"
    )
    assert history == [
        (1, "Original.", None),
        (2, "Edited meanwhile.", None),
        (3, "Edited meanwhile.", date(2026, 4, 30)),
    ]
    again = await store.put(NoteUnit(_note("successor", "The new one."), retirements=(retirement,)))
    assert again.written == () and again.revisions["target"] == 3


async def test_a_retirement_that_cannot_close_the_note_as_it_now_stands_fails_the_whole_unit(
    store: PostgresNoteStore,
) -> None:
    """The note began after the date the retirement closes it on: nothing of the unit lands."""
    await store.put(NoteUnit(_note("target", "Original.")))
    read = await store.get("target")
    assert read is not None
    retirement = read.model_copy(update={"valid_to": date(2026, 1, 1)})
    await store.put(NoteUnit(_note("target", "Moved.", valid_from=date(2026, 6, 1))))
    with pytest.raises(NoteWriteError):
        await store.put(NoteUnit(_note("successor", "New."), retirements=(retirement,)))
    assert await store.get("successor") is None


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
    """Out-edges by the primary key, in-edges by `kg_edges_dst_idx`, a term by the trigram index.

    The search is asked without its tenant filter: at this size the planner prefers the tenant's
    index to the trigram one, and the question is whether the trigram index can serve `LIKE`.
    """
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
            "SELECT id FROM kg_notes WHERE search_text LIKE %s",
            ("%number 77%",),
        )
        == "kg_notes_search_text_trgm_idx"
    )


# --- The unit is the order the file writer uses -------------------------------------------------


def _old_file_order(
    note: Note, dependencies: list[Note], superseded: list[Note]
) -> list[tuple[str, str]]:
    """`record._build_write`'s order before it was built from `NoteUnit`, frozen as it was.

    `(role, path)` of each file: dependencies that are not already seen, the subject, then the
    retirements that are not already seen.
    """

    def path(member: Note) -> str:
        return f"knowledge/{note_relative_path(member.type, member.id)}"

    seen = {note.id}
    files = []
    for dependency in dependencies:
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        files.append(("dependency", path(dependency)))
    files.append(("subject", path(note)))
    for retired in superseded:
        if retired.id in seen:
            continue
        seen.add(retired.id)
        files.append(("retirement", path(retired)))
    return files


async def test_the_file_write_keeps_the_order_it_had_before_it_was_built_from_a_unit() -> None:
    """Random units over few ids, so repeats and collisions with the subject occur; same files."""
    rng = random.Random(11)
    pool = [f"n{i}" for i in range(5)]

    def some() -> list[Note]:
        return [
            Note(id=rng.choice(pool), type=rng.choice(["reaction", "compound"]), created_by="agent")
            for _ in range(rng.randrange(0, 5))
        ]

    for _ in range(400):
        subject = Note(id=rng.choice(pool), type="playbook", created_by="agent")
        dependencies, retirements = some(), some()
        expected = _old_file_order(subject, dependencies, retirements)
        write = _build_write(subject, "knowledge", dependencies or None, retirements or None)
        assert [(f.path, f.overwrite, f.amendment) for f in write.files] == [
            (path, role != "dependency", role == "retirement") for role, path in expected
        ]
        unit = NoteUnit(subject, tuple(dependencies), tuple(retirements))
        assert [
            (role, f"knowledge/{note_relative_path(n.type, n.id)}") for role, n in unit.ordered()
        ] == expected
